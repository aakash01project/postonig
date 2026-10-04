"""Daily Yoga post -> Instagram (semi-manual queue mode, NO AI at post time).

How it works
  * You put images in the repo folder `queue/` named by number:  01.png, 02.png ... (01-tadasana.png is fine too).
  * Every day at 7:12 AM the oldest number is taken, its Hindi one-liner + caption come from content.json
    (already reviewed by you), the Hindi text is printed on the image, and it is posted to Instagram.
  * The used image is removed from queue/. When 2 or fewer images are left, a GitHub Issue with the
    next 10 image prompts is opened (GitHub e-mails you) so you can make the next batch.

SAFE RETRY RULES
  * Only temporary problems (network, HTTP 408/429/5xx) are retried, max 3 tries, waits 5s,10s.
  * 401/403/404 etc. stop at once. Instagram "publish" is NEVER retried -> no double posts.
"""
import base64
import datetime
import glob
import io
import json
import os
import re
import sys
import time

import requests
from PIL import Image, ImageDraw, ImageFont, features

IG_USER_ID = os.environ["IG_USER_ID"]
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]  # "user/repo" (auto-set in Actions)
GRAPH = "https://graph.instagram.com/v21.0"  # Instagram Login (no Facebook Page needed)

MAX_TRIES = 3
IG_PUBLISH_TRIES = 1  # never retry publishing: avoids double posts
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
LOW_QUEUE = 2  # open the "send me new prompts" issue when this many (or fewer) images are left
BATCH = 10
W, H = 1080, 1350  # Instagram 4:5 portrait

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "content.json"), encoding="utf-8") as _f:
    CONTENT = json.load(_f)
N = len(CONTENT)
GH = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"


def entry_for(num):
    """Image number 1..60 -> asana. After 60 the series starts again (61 -> 1)."""
    return CONTENT[(num - 1) % N]


# ---------------------------------------------------------------- safe retry helpers
class FatalError(Exception):
    """Do not retry - stop the run with a clear message."""


class TransientError(Exception):
    """Temporary problem - may be retried (within the limit)."""


def send(method, url, what, **kw):
    try:
        r = requests.request(method, url, **kw)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise TransientError(f"{what}: network problem ({e.__class__.__name__})")
    if r.status_code < 400:
        return r
    msg = f"{what}: HTTP {r.status_code}: {r.text[:300]}"
    if r.status_code in RETRY_STATUS:
        raise TransientError(msg)
    raise FatalError(msg)


def retry(fn, what, tries=MAX_TRIES, base_delay=5):
    for i in range(1, tries + 1):
        try:
            return fn()
        except TransientError as e:
            print(f"{what}: try {i}/{tries} failed - {e}")
            if i == tries:
                raise FatalError(f"{what}: giving up after {tries} tries - {e}")
            time.sleep(base_delay * 2 ** (i - 1))


# ---------------------------------------------------------------- queue
def list_queue():
    """Return [(number, file_info)] sorted by number. Empty list if folder missing/empty."""
    try:
        r = retry(lambda: send("GET", f"{API}/contents/queue", "Queue list", headers=GH, timeout=30), "Queue list")
    except FatalError as e:
        if "HTTP 404" in str(e):
            return []
        raise
    items = []
    for f in r.json():
        if f["type"] != "file" or not f["name"].lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            continue
        m = re.match(r"\s*(\d+)", f["name"])
        if not m:
            raise FatalError(f"File `{f['name']}` ka naam number se shuru hona chahiye (jaise 01.png).")
        items.append((int(m.group(1)), f))
    return sorted(items, key=lambda x: x[0])


def load_queue_image(f):
    r = retry(lambda: send("GET", f["download_url"], "Queue image download", timeout=60), "Queue image download")
    try:
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        raise FatalError(f"`{f['name']}` image khul nahi rahi (kharab file?)")
    scale = max(W / img.width, H / img.height)  # fill the 4:5 frame, crop the extra
    img = img.resize((round(img.width * scale), round(img.height * scale)))
    left, top = (img.width - W) // 2, (img.height - H) // 2
    return img.crop((left, top, left + W, top + H))


def remove_from_queue(f):
    try:
        requests.delete(f"{API}/contents/{f['path']}", headers=GH, timeout=30,
                        json={"message": f"Used {f['name']}", "sha": f["sha"]}).raise_for_status()
    except requests.RequestException as e:
        print("WARNING: queue image delete nahi hui, haath se hata do:", e)


# ---------------------------------------------------------------- state (last posted number + date)
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def today_ist():
    return datetime.datetime.now(IST).date().isoformat()


def read_state():
    try:
        r = requests.get(f"{API}/contents/state.json", headers=GH, timeout=30)
        if r.ok:
            return json.loads(base64.b64decode(r.json()["content"]))
    except (requests.RequestException, ValueError, KeyError):
        pass
    return {}


def read_last_n():
    try:
        return int(read_state().get("last_n", 0))
    except (TypeError, ValueError):
        return 0


def write_last_n(n):
    try:
        data = {"last_n": n, "last_date": today_ist()}
        payload = {"message": f"Posted #{n}", "content": base64.b64encode(json.dumps(data).encode()).decode()}
        old = requests.get(f"{API}/contents/state.json", headers=GH, timeout=30)
        if old.ok:
            payload["sha"] = old.json()["sha"]
        requests.put(f"{API}/contents/state.json", headers=GH, json=payload, timeout=30).raise_for_status()
    except requests.RequestException as e:
        print("WARNING: state.json update nahi hui:", e)


# ---------------------------------------------------------------- "send me prompts" issue
def build_issue_body(start):
    owner = GITHUB_REPOSITORY.split("/")[0]
    lines = [
        f"@{owner} queue me images kam bachi hain. Neeche agle {BATCH} asana ke image prompts hain.",
        "",
        "ChatGPT me ek-ek prompt daalo, pose sahi hai ya nahi dekho (galat ho to dobara banwao), "
        "image ko `01.png` jaise **number** wale naam se repo ke `queue/` folder me upload karo.",
        "",
    ]
    for num in range(start, start + BATCH):
        e = entry_for(num)
        lines += [f"### `{num:02d}.png` - {e['en']} ({e['hi']})", "```", e["prompt"], "```", ""]
    return "\n".join(lines)


def notify_prompts(start):
    title = f"Naye image prompts chahiye: {start}-{start + BATCH - 1}"
    try:
        open_issues = requests.get(f"{API}/issues", headers=GH, params={"state": "open", "per_page": 50}, timeout=30)
        if open_issues.ok and any(i["title"].startswith("Naye image prompts chahiye") for i in open_issues.json()):
            print("Prompt issue already open - not creating another.")
            return
        requests.post(f"{API}/issues", headers=GH, timeout=30,
                      json={"title": title, "body": build_issue_body(start)}).raise_for_status()
        print("Issue created:", title)
    except requests.RequestException as e:
        print("WARNING: issue nahi ban paya:", e)


# ---------------------------------------------------------------- caption + image text
def build_caption(e):
    lines = [f"🧘 {e['hi']} ({e['en']})", "", "✨ Fayde (Benefits):"]
    lines += [f"✅ {p}" for p in e["points"]]
    if e.get("how"):
        lines += ["", f"📝 Kaise kare: {e['how']}"]
    lines += [
        "",
        "⚠️ Dhyan rahe: Yoga apne sharir ke hisaab se kare. Koi bimari, chot ya pregnancy ho "
        "to pehle doctor ya yoga trainer se salaah le.",
        "",
        f"#{re.sub('[^A-Za-z0-9]', '', e['en'])} #arerajyogaclub #areraj #DailyYoga #Yoga",
        "",
        "AI-assisted • Verify if needed.",
    ]
    return "\n".join(lines)[:2200]


def load_font(size, bold=True):
    """Devanagari font: Noto Sans Devanagari (installed by the workflow); FreeSans/Lohit as backup."""
    raq = ImageFont.Layout.RAQM
    noto = sorted(glob.glob("/usr/share/fonts/**/NotoSansDevanagari*.ttf", recursive=True))
    noto = [p for p in noto if not any(x in p for x in ("UI", "Condensed", "Semi", "Extra"))]
    want = "Bold" if bold else "Medium"
    static = [p for p in noto if "[" not in p and want in os.path.basename(p)]
    if not static and not bold:
        static = [p for p in noto if "[" not in p and "Regular" in os.path.basename(p)]
    if static:
        return ImageFont.truetype(static[0], size, layout_engine=raq)
    variable = [p for p in noto if "[" in p]
    if variable:  # variable font: choose the weight by name
        f = ImageFont.truetype(variable[0], size, layout_engine=raq)
        try:
            f.set_variation_by_name(want)
        except Exception:
            pass
        return f
    backup = (glob.glob("/usr/share/fonts/**/FreeSansBold.ttf", recursive=True)
              + glob.glob("/usr/share/fonts/**/Lohit-Devanagari.ttf", recursive=True))
    if not backup:
        raise FatalError("No Devanagari font found (install fonts-noto-core).")
    return ImageFont.truetype(backup[0], size, layout_engine=raq)


def wrap(draw, text, font, max_width):
    lines, cur = [], ""
    for w in text.split():
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font, language="hi") <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# Colours: calm deep-green fade, warm sand-gold title, soft off-white text (natural, trustworthy feel)
BAND_RGB = (22, 40, 34)
TITLE_COLOR = (244, 221, 169)
BODY_COLOR = (250, 247, 240)


def add_hindi_text(img, pose_hi, benefit_hi):
    """Hindi title + one-line benefit. The dark green fade adapts itself to the picture: the
    brighter the picture is behind the text, the stronger the fade, so text is readable on ANY colour."""
    from PIL import ImageFilter, ImageStat
    if not features.check("raqm"):
        raise FatalError("Pillow has no raqm support: Hindi text would render incorrectly.")
    base = img.convert("RGBA")
    d = ImageDraw.Draw(base)

    size = 108  # shrink long titles so they never run off the image
    title_font = load_font(size, bold=True)
    while d.textlength(pose_hi, font=title_font, language="hi") > W - 160 and size > 60:
        size -= 4
        title_font = load_font(size, bold=True)
    body_font = load_font(52, bold=False)  # lighter weight than the title = calmer, easier to read
    body_lines = wrap(d, benefit_hi, body_font, W - 170)
    line_h = 80
    total_h = 185 + line_h * len(body_lines)
    y0 = H - 95 - total_h

    band_h = 700
    top = H - band_h
    out = base
    for strength in (1.0, 1.15, 1.3, 1.5):  # darken more until the text area is dark enough
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        for i in range(band_h):
            t = i / band_h
            alpha = min(245, int(235 * strength * min(1.0, t / 0.5) ** 1.1))
            od.line([(0, top + i), (W, top + i)], fill=BAND_RGB + (alpha,))
        out = Image.alpha_composite(base, overlay)
        zone = out.convert("L").crop((0, y0 - 25, W, H))
        if ImageStat.Stat(zone).mean[0] <= 85:  # 0 = black, 255 = white
            break

    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.text((W // 2, y0 + 3), pose_hi, font=title_font, fill=(0, 0, 0, 170), anchor="ma", language="hi")
    yy = y0 + 175
    for ln in body_lines:
        sd.text((W // 2, yy + 2), ln, font=body_font, fill=(0, 0, 0, 150), anchor="ma", language="hi")
        yy += line_h
    out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(5)))
    d = ImageDraw.Draw(out)

    d.text((W // 2, y0), pose_hi, font=title_font, fill=TITLE_COLOR, anchor="ma", language="hi")
    d.rounded_rectangle((W // 2 - 55, y0 + 150, W // 2 + 55, y0 + 154), radius=2, fill=TITLE_COLOR)
    y = y0 + 175
    for ln in body_lines:
        d.text((W // 2, y), ln, font=body_font, fill=BODY_COLOR, anchor="ma", language="hi")
        y += line_h
    return add_logo(out.convert("RGB"))


LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "instagram_logo.png")


def add_logo(img):
    """Optional: put YOUR official Instagram logo file (assets/instagram_logo.png) in the top-left corner.
    If the file is not in the repo, nothing is added."""
    if not os.path.exists(LOGO_PATH):
        return img
    try:
        logo = Image.open(LOGO_PATH).convert("RGBA")
        logo = logo.resize((52, max(1, round(52 * logo.height / logo.width))))
        img = img.convert("RGBA")
        img.paste(logo, (36, 36), logo)  # top-left corner, so it never overlaps the header text
        return img.convert("RGB")
    except Exception:
        return img


def to_jpeg(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


# ---------------------------------------------------------------- GitHub upload + Instagram
def upload_to_github(jpeg_bytes):
    name = f"images/{datetime.date.today().isoformat()}.jpg"
    api = f"{API}/contents/{name}"
    payload = {"message": f"Add image {name}", "content": base64.b64encode(jpeg_bytes).decode()}
    try:  # 404 only means "file does not exist yet"
        existing = requests.get(api, headers=GH, timeout=30)
        if existing.ok:
            payload["sha"] = existing.json()["sha"]
    except requests.RequestException:
        pass
    retry(lambda: send("PUT", api, "GitHub upload", headers=GH, json=payload, timeout=60), "GitHub upload")
    raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{name}"
    for _ in range(10):  # wait (max ~50s) until the URL is reachable
        try:
            if requests.head(raw_url, timeout=20).status_code == 200:
                return raw_url
        except requests.RequestException:
            pass
        time.sleep(5)
    raise FatalError("Uploaded image URL is not reachable (is the repo public?)")


def post_to_instagram(image_url, caption):
    r = retry(
        lambda: send("POST", f"{GRAPH}/{IG_USER_ID}/media", "Instagram create",
                     data={"image_url": image_url, "caption": caption, "access_token": IG_ACCESS_TOKEN}, timeout=60),
        "Instagram create", 2,
    )
    container_id = r.json()["id"]
    for _ in range(20):  # wait (max ~100s) for processing
        try:
            s = requests.get(f"{GRAPH}/{container_id}",
                             params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN}, timeout=30).json()
        except (requests.RequestException, ValueError):
            s = {}
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") == "ERROR":
            raise FatalError(f"Instagram processing error: {s}")
        time.sleep(5)
    r = retry(  # publish exactly ONCE
        lambda: send("POST", f"{GRAPH}/{IG_USER_ID}/media_publish", "Instagram publish",
                     data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN}, timeout=60),
        "Instagram publish", IG_PUBLISH_TRIES,
    )
    return r.json()["id"]


def main():
    dry = os.environ.get("DRY_RUN", "").lower() == "true"
    force = os.environ.get("FORCE", "").lower() == "true"
    state = read_state()
    # ONE post per day: applies to scheduled AND manual runs. Tick "force" in Run workflow to post again on purpose.
    if not dry and not force and state.get("last_date") == today_ist():
        print("Aaj ka post ho chuka hai - is run me kuch nahi karna. (Dobara karna ho to Run workflow me 'force' tick karo.)")
        return
    queue = list_queue()
    if not queue:
        notify_prompts(read_last_n() + 1)
        raise FatalError("Queue khali hai: `queue/` folder me nayi image daalo (GitHub Issue me prompts bhej diye hain).")
    num, f = queue[0]
    last_n = read_last_n()
    if not dry and not force and num <= last_n:
        raise FatalError(f"`{f['name']}` (number {num}) pehle hi post ho chuki hai (last_n = {last_n}). "
                         "Is file ko `queue/` se hata do, ya naya number do. Zabardasti post karni ho to 'force' tick karo.")
    e = entry_for(num)
    print(f"Image #{num}: {f['name']} -> {e['en']} ({e['hi']})")
    img = add_hindi_text(load_queue_image(f), e["hi"], e["line"])
    caption = build_caption(e)
    if dry:
        # TEST MODE: nothing is posted, uploaded, deleted or saved. Look at the files in the run's "Artifacts".
        with open("preview.jpg", "wb") as pf:
            pf.write(to_jpeg(img))
        with open("preview_caption.txt", "w", encoding="utf-8") as cf:
            cf.write(caption)
        print("DRY RUN: preview.jpg aur preview_caption.txt ban gayi. Instagram par kuch post nahi hua.")
        print("----- caption -----")
        print(caption)
        return
    url = upload_to_github(to_jpeg(img))
    print("Image URL:", url)
    post_id = post_to_instagram(url, caption)
    print("Posted! Media ID:", post_id)
    remove_from_queue(f)
    write_last_n(num)
    rest = queue[1:]
    if len(rest) <= LOW_QUEUE:
        notify_prompts(max([num] + [n for n, _ in rest]) + 1)


if __name__ == "__main__":
    try:
        main()
    except FatalError as e:
        sys.exit(f"STOPPED: {e}")
