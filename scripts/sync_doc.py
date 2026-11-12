#!/usr/bin/env python3
"""Pull the site's content from a Google Doc and write content.json (+ images/).

Google's HTML export always returns the WHOLE document (every tab) in one
go - there's no URL parameter that scopes it to a single tab. So instead we
fetch it once and split the result ourselves: every tab starts with its own
Title-styled paragraph, so a new Title paragraph marks the start of the next
tab. Tab 1 becomes content.json (the home page); tab 2, if present, becomes
blog.json, rendered by blog.html as its own page.

The doc must be shared as "Anyone with the link can view". Doc layout:
  Title            -> site name            Subtitle -> tagline
  Image before the first Heading 1 -> profile photo
  Text before the first Heading 1  -> intro
  Heading 1        -> a section (e.g. "interests")
  Heading 2        -> a sub-section inside it (e.g. "mountains")
  Plain text       -> blurb under that subheading
  Image            -> a post-it; text in the same paragraph is its caption
  Pasted picture LINK (postimg, imgur, Google Drive share link, .jpg/.png url...)
                   -> treated exactly like an inserted image
  "post-it: caption" (no image) -> an empty post-it placeholder
A section called "find me" / "links" / "contact" renders as a row of links.
"""
import hashlib, json, os, re, shutil, subprocess, sys, urllib.parse, urllib.request
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (site-sync)"}


def fetch(url, image_only=False):
    """Download a URL. With image_only, a non-image response (a web page, or a big
    video file) is only read as far as needed to look for an og:image tag."""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        ctype = r.headers.get("Content-Type", "")
        if image_only and not ctype.lower().startswith("image/"):
            return r.read(400000), ctype
        return r.read(), ctype

def unwrap(href):
    """Google wraps outbound links as google.com/url?q=<real>."""
    p = urllib.parse.urlparse(href)
    if p.netloc.endswith("google.com") and p.path == "/url":
        q = urllib.parse.parse_qs(p.query).get("q")
        if q:
            return q[0]
    return href


class DocParser(HTMLParser):
    BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.css = ""
        self.in_style = self.in_head = False
        self.block = None          # current open block
        self.span_classes = []     # stack of class strings for open inline tags
        self.href = None
        self.blocks = []           # finished blocks

    def style_flags(self, classes):
        b = i = False
        for c in classes.split():
            m = re.search(r"\.%s\{([^}]*)\}" % re.escape(c), self.css)
            if m:
                b = b or "font-weight:700" in m.group(1)
                i = i or "font-style:italic" in m.group(1)
        return b, i

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "head": self.in_head = True
        elif tag == "style": self.in_style = True
        elif tag in self.BLOCKS and self.block is None:
            cls = a.get("class", "").split()
            kind = tag
            if "title" in cls: kind = "title"
            elif "subtitle" in cls: kind = "subtitle"
            self.block = {"kind": kind, "runs": [], "images": []}
        elif tag == "img" and self.block is not None and a.get("src"):
            self.block["images"].append({"src": a["src"], "alt": a.get("alt", "")})
        elif tag == "br" and self.block is not None:
            self.block["runs"].append({"text": " "})
        if tag == "a":
            self.href = unwrap(a["href"]) if a.get("href") else None
        if tag in ("span", "a", "b", "i", "em", "strong"):
            self.span_classes.append(a.get("class", ""))

    def handle_endtag(self, tag):
        if tag == "head": self.in_head = False
        elif tag == "style": self.in_style = False
        elif tag in ("span", "a", "b", "i", "em", "strong") and self.span_classes:
            self.span_classes.pop()
        if tag == "a": self.href = None
        if tag in self.BLOCKS and self.block is not None:
            self.blocks.append(self.block)
            self.block = None

    def handle_data(self, data):
        if self.in_style:
            self.css += data
        elif self.block is not None and data:
            b, i = self.style_flags(" ".join(self.span_classes))
            run = {"text": data}
            if self.href: run["href"] = self.href
            if b: run["b"] = True
            if i: run["i"] = True
            self.block["runs"].append(run)


def tidy(runs):
    """Merge adjacent identical-style runs, trim the ends."""
    out = []
    for r in runs:
        if out and {k: v for k, v in out[-1].items() if k != "text"} == {k: v for k, v in r.items() if k != "text"}:
            out[-1]["text"] += r["text"]
        else:
            out.append(dict(r))
    while out and not out[0]["text"].strip() and "href" not in out[0]: out.pop(0)
    while out and not out[-1]["text"].strip() and "href" not in out[-1]: out.pop()
    if out:
        out[0]["text"] = out[0]["text"].lstrip()
        out[-1]["text"] = out[-1]["text"].rstrip()
    return [r for r in out if r["text"]]


def plain(runs):
    return " ".join("".join(r["text"] for r in runs).split())


IMG_EXT = re.compile(r"\.(jpe?g|png|gif|webp|avif)(\?|#|$)", re.I)
IMG_HOSTS = ("postimg.cc", "imgur.com", "ibb.co", "pinimg.com", "googleusercontent.com", "drive.google.com",
             "photos.google.com", "photos.app.goo.gl", "cloudinary.com", "unsplash.com", "flickr.com", "staticflickr.com")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def is_image_link(url):
    u = urllib.parse.urlparse(url)
    host = u.netloc.lower()
    return bool(IMG_EXT.search(u.path + ("?" + u.query if u.query else ""))) or any(
        host == h or host.endswith("." + h) for h in IMG_HOSTS)


def direct_url(url):
    """Turn a share link into one that serves the picture itself."""
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?[^#]*id=)([\w-]+)", url)
    if m:
        return "https://drive.google.com/uc?export=download&id=" + m.group(1)
    return url


def drive_id(url):
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?[^#]*id=)([\w-]+)", url or "")
    return m.group(1) if m else None


def og_image(data):
    """Pull a page's og:image thumbnail URL out of its raw bytes, if any."""
    page = data.decode("utf-8", "replace")
    m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page, re.I)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', page, re.I))
    return m.group(1) if m else None


class NotAPicture(Exception):
    pass


def fetch_image(url):
    data, ctype = fetch(direct_url(url), image_only=True)
    if ctype.lower().startswith("image/"):
        return data, ctype
    if drive_id(url):
        # a Drive file that isn't a picture is a video - the site plays it with Drive's own player
        raise NotAPicture(ctype or "not an image")
    # a web page that shows the picture (e.g. postimg.cc/xxxx): use its og:image
    thumb = og_image(data)
    if not thumb:
        raise ValueError("that link is a web page, not a picture (the picture may be private)")
    import html as _html
    data, ctype = fetch(_html.unescape(thumb))
    if not ctype.lower().startswith("image/"):
        raise ValueError("could not get a picture from that link")
    return data, ctype


def split_image_links(runs):
    """Pull picture links out of a paragraph. Returns (remaining runs, [urls])."""
    urls, rest = [], []
    for r in runs:
        href = r.get("href")
        if href and is_image_link(href):
            urls.append(href)
            label = r["text"].strip()
            # a pasted URL or a Drive chip's file name ("IMG_1234.jpg") is not a caption; real link text is
            if not URL_RE.fullmatch(label) and not re.search(r"\.(jpe?g|png|gif|webp|avif|heic)$", label, re.I):
                rest.append({"text": r["text"]})
            continue

        def grab(m):
            if is_image_link(m.group(0)):
                urls.append(m.group(0))
                return " "
            return m.group(0)
        r2 = dict(r)
        r2["text"] = URL_RE.sub(grab, r["text"])
        rest.append(r2)
    return tidy(rest), urls


VIDEO = "video"   # what save_image returns for a Drive video: there's no picture to save


def save_image(url, used):
    ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
    name = hashlib.sha1(url.encode()).hexdigest()[:12]
    for ext in ext_map.values():          # already downloaded
        if os.path.exists(os.path.join(ROOT, "images", name + ext)):
            used.add(name + ext)
            return "images/" + name + ext
    try:
        data, ctype = fetch_image(url)
    except NotAPicture:
        print("video: %s (embedded with Drive's player)" % url)
        return VIDEO
    except Exception as e:
        print("WARNING: could not load picture %s -> %s" % (url, e))
        return None
    ext = ext_map.get(ctype.split(";")[0].strip(), ".png")
    os.makedirs(os.path.join(ROOT, "images"), exist_ok=True)
    with open(os.path.join(ROOT, "images", name + ext), "wb") as f:
        f.write(data)
    used.add(name + ext)
    print("saved picture %s (%d KB) from %s" % (name + ext, len(data) // 1024, url))
    return "images/" + name + ext


MAX_VIDEO_OUT = 25 * 1024 * 1024        # a converted clip bigger than this isn't worth keeping in the repo
MAX_VIDEO_RAW_FFMPEG = 150 * 1024 * 1024  # biggest original we'll download when we can shrink it
MAX_VIDEO_RAW_PLAIN = 25 * 1024 * 1024    # ...and when we can't


def _sniff_video(path):
    with open(path, "rb") as f:
        head = f.read(12)
    return head[4:8] == b"ftyp" or head[:4] == b"\x1a\x45\xdf\xa3"     # mp4/mov or webm


def download_video(file_id, cap):
    """Stream a Drive video to a temp file in images/. None if it isn't a real video or is over the cap."""
    os.makedirs(os.path.join(ROOT, "images"), exist_ok=True)
    tmp = os.path.join(ROOT, "images", ".tmp-" + file_id)
    for url in ("https://drive.usercontent.google.com/download?id=%s&export=download&confirm=t" % file_id,
                "https://drive.google.com/uc?export=download&id=%s" % file_id):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                ctype = r.headers.get("Content-Type", "").lower()
                if not (ctype.startswith("video/") or ctype.startswith("application/octet-stream")):
                    continue
                size = 0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > cap:
                            raise ValueError("bigger than %d MB" % (cap // 1048576))
                        f.write(chunk)
            if _sniff_video(tmp):
                return tmp
        except Exception as e:
            print("  could not download video %s: %s" % (file_id, e))
        if os.path.exists(tmp):
            os.remove(tmp)
    return None


def save_video(file_id, used):
    """Download a Drive video into images/ as a small, widely-playable mp4 (+ a poster frame)
    so the site can autoplay it. Returns {"vsrc", "poster"} or None (then the site uses Drive's player)."""
    name = "v-" + hashlib.sha1(file_id.encode()).hexdigest()[:12]
    mp4, jpg = name + ".mp4", name + ".jpg"
    imgdir = os.path.join(ROOT, "images")
    if os.path.exists(os.path.join(imgdir, mp4)):                      # already converted on an earlier sync
        used.add(mp4)
        has_poster = os.path.exists(os.path.join(imgdir, jpg))
        if has_poster:
            used.add(jpg)
        return {"vsrc": "images/" + mp4, "poster": "images/" + jpg if has_poster else None}
    ffmpeg = shutil.which("ffmpeg")
    tmp = download_video(file_id, MAX_VIDEO_RAW_FFMPEG if ffmpeg else MAX_VIDEO_RAW_PLAIN)
    if not tmp:
        return None
    dest = os.path.join(imgdir, mp4)
    try:
        converted = False
        if ffmpeg:
            try:
                subprocess.run([ffmpeg, "-y", "-v", "error", "-i", tmp, "-vf", "scale=-2:min(ih\\,720)",
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
                                "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dest],
                               check=True, capture_output=True, timeout=900)
                converted = True
            except Exception as e:
                print("  ffmpeg could not convert %s: %s" % (file_id, e))
                if os.path.exists(dest):
                    os.remove(dest)
        if not converted:
            if os.path.getsize(tmp) > MAX_VIDEO_OUT:
                return None
            shutil.copyfile(tmp, dest)
        if os.path.getsize(dest) > MAX_VIDEO_OUT:
            print("  video %s is still over 25 MB after converting - using Drive's player instead" % file_id)
            os.remove(dest)
            return None
        poster = None
        if ffmpeg:
            try:
                subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", "0.3", "-i", dest, "-frames:v", "1",
                                "-vf", "scale=-2:min(ih\\,540)", "-q:v", "5", os.path.join(imgdir, jpg)],
                               check=True, capture_output=True, timeout=120)
                poster = "images/" + jpg
                used.add(jpg)
            except Exception:
                poster = None
        used.add(mp4)
        print("saved video %s (%d KB) from Drive file %s" % (mp4, os.path.getsize(dest) // 1024, file_id))
        return {"vsrc": "images/" + mp4, "poster": poster}
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def split_tabs(blocks):
    """Google's export concatenates every tab into one blocks list. Each tab
    starts with its own Title-styled paragraph, so split there."""
    tabs = []
    for blk in blocks:
        if blk["kind"] == "title" and tabs:
            tabs.append([blk])
        else:
            if not tabs:
                tabs.append([])
            tabs[-1].append(blk)
    return tabs


def build(blocks, localize, localize_video=None):
    content = {"title": "", "subtitle": "", "avatar": None, "intro": [], "sections": []}
    section = None
    for blk in blocks:
        runs = tidy(blk["runs"])
        text = plain(runs)
        kind = blk["kind"]
        if kind == "title":
            content["title"] = text or content["title"]
        elif kind == "subtitle":
            content["subtitle"] = text
        elif kind in ("h1", "h2", "h3", "h4", "h5", "h6"):
            # any heading level starts a new section; h1 is a top-level section,
            # anything deeper (h2..h6) is a sub-section under it.
            if text:
                section = {"title": text, "level": 1 if kind == "h1" else 2, "blocks": []}
                content["sections"].append(section)
        else:
            runs, link_urls = split_image_links(runs)
            text = plain(runs)
            if link_urls or blk["images"]:
                # never keep a picture's file name: "IMG_1.jpg: lake" / "lake IMG_1.jpg" -> "lake"
                text = re.sub(r"\b(?:WhatsApp (?:Image|Video)|Screenshot|IMG|DSC|DSCN|PXL|VID|PHOTO|Photo|image)[\w\-. ()]*?\.(?:jpe?g|png|gif|webp|avif|heic)\b", "", text, flags=re.I)
                text = re.sub(r"^.*?\.(?:jpe?g|png|gif|webp|avif|heic)\b(?=[\s:;\-–—·]+\S)[\s:;\-–—·]*", "", text, flags=re.I)
                text = re.sub(r"\S+\.(?:jpe?g|png|gif|webp|avif|heic)\b", "", text, flags=re.I)
                text = re.sub(r"^[\s:;\-–—·]+", "", " ".join(text.split()))   # "link: caption" -> "caption"
            blk = dict(blk, images=list(blk["images"]) + [{"src": u, "alt": "", "link": u} for u in link_urls])
            for img in blk["images"]:
                src = localize(img["src"])
                if src == VIDEO:
                    # a Drive video: no picture to download, the site embeds Drive's player
                    if section is not None:
                        did = drive_id(img.get("link") or img["src"])
                        note = {"type": "note", "src": "", "alt": img["alt"], "caption": text,
                                "link": img.get("link") or img["src"], "video": True, "drive": did}
                        local = localize_video(did) if (localize_video and did) else None
                        if local:
                            note["vsrc"] = local["vsrc"]
                            if local.get("poster"):
                                note["poster"] = local["poster"]
                        section["blocks"].append(note)
                    continue
                if not src:
                    continue
                if section is None:
                    content["avatar"] = content["avatar"] or {"src": src, "alt": img["alt"] or text}
                else:
                    note = {"type": "note", "src": src, "alt": img["alt"], "caption": text}
                    if img.get("link"):
                        note["link"] = img["link"]
                    section["blocks"].append(note)
            m = re.match(r"\s*post-?it\s*:\s*(.*)$", text, re.I)
            if m and not blk["images"] and section is not None:
                section["blocks"].append({"type": "note", "src": "", "alt": "", "caption": m.group(1)})
            elif text and not blk["images"]:
                if section is None:
                    content["intro"].append(runs)
                else:
                    section["blocks"].append({"type": "p", "runs": runs})
    return content


def sync_one(blocks, out_name, used, required):
    """Build <out_name>.json from one tab's already-parsed blocks.
    Images (and pasted video links) are downloaded into images/ and added to
    the shared `used` set so the final cleanup pass doesn't delete a picture
    that's still referenced by another page."""
    stats = {"ok": 0, "video": 0, "failed": 0}

    def localize(u):
        src = save_image(u, used)
        stats["video" if src == VIDEO else "ok" if src else "failed"] += 1
        return src
    content = build(blocks, localize, lambda fid: save_video(fid, used))
    notes = [b for s in content["sections"] for b in s["blocks"] if b["type"] == "note"]
    print("[%s] Found %d sections, %d post-its (%d with a picture), profile photo: %s" % (
        out_name, len(content["sections"]), len(notes), sum(1 for n in notes if n["src"] or n.get("video")),
        "yes" if content["avatar"] else "NO"))
    print("[%s] Pictures downloaded: %d, videos embedded: %d, failed: %d" % (
        out_name, stats["ok"], stats["video"], stats["failed"]))
    if not content["sections"]:
        if required:
            print("[%s] Doc parsed but has no Heading 1 sections - leaving %s.json untouched." % (out_name, out_name))
            return False
        print("[%s] This tab has no Heading 1 sections - leaving %s.json untouched." % (out_name, out_name))
        return True
    out = json.dumps(content, indent=2, ensure_ascii=False) + "\n"
    path = os.path.join(ROOT, out_name + ".json")
    if not os.path.exists(path) or open(path, encoding="utf-8").read() != out:
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
        print("[%s] %s.json updated." % (out_name, out_name))
    else:
        print("[%s] No changes." % out_name)
    return True


def main():
    with open(os.path.join(ROOT, "site.config.json")) as f:
        config = json.load(f)
    doc_id = config.get("docId", "")
    if not doc_id or doc_id.startswith("PASTE_"):
        print("site.config.json has no docId yet - nothing to sync.")
        return 0

    url = "https://docs.google.com/document/d/%s/export?format=html" % doc_id
    try:
        raw, _ = fetch(url)
    except Exception as e:
        print("Could not fetch the doc (is it shared as 'Anyone with the link can view'?):", e)
        return 1
    html = raw.decode("utf-8", "replace")
    print("Fetched: %d characters, %d Drive links, %d <img> tags, %d image-host links" % (
        len(html), len(re.findall(r"drive\.google\.com", html)), len(re.findall(r"<img\b", html)),
        len(re.findall(r"postimg|imgur|ibb\.co", html))))

    parser = DocParser()
    parser.feed(html)
    tabs = split_tabs(parser.blocks)
    print("Found %d tab(s) in the doc." % len(tabs))

    used = set()
    ok = sync_one(tabs[0] if tabs else [], "content", used, required=True)
    if len(tabs) > 1:
        sync_one(tabs[1], "blog", used, required=False)
    else:
        print("[blog] Doc only has one tab - leaving blog.json untouched.")

    for fn in os.listdir(os.path.join(ROOT, "images")) if os.path.isdir(os.path.join(ROOT, "images")) else []:
        if fn not in used and fn != ".gitkeep":
            os.remove(os.path.join(ROOT, "images", fn))

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
