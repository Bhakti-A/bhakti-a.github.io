#!/usr/bin/env python3
"""Pull the site's content from a Google Doc and write content.json (+ images/).

The doc must be shared as "Anyone with the link can view". Doc layout:
  Title            -> site name            Subtitle -> tagline
  Image before the first Heading 1 -> profile photo
  Text before the first Heading 1  -> intro
  Heading 1        -> an interest section (its own subheading on the page)
  Plain text       -> blurb under that subheading
  Image            -> a post-it; text in the same paragraph is its caption
A section called "find me" / "links" / "contact" renders as a row of links.
"""
import hashlib, json, os, re, sys, urllib.parse, urllib.request
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (site-sync)"}


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(), r.headers.get("Content-Type", "")


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


def save_image(url, used):
    ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
    name = hashlib.sha1(url.encode()).hexdigest()[:12]
    for ext in ext_map.values():          # already downloaded
        if os.path.exists(os.path.join(ROOT, "images", name + ext)):
            used.add(name + ext)
            return "images/" + name + ext
    data, ctype = fetch(url)
    ext = ext_map.get(ctype.split(";")[0].strip(), ".png")
    os.makedirs(os.path.join(ROOT, "images"), exist_ok=True)
    with open(os.path.join(ROOT, "images", name + ext), "wb") as f:
        f.write(data)
    used.add(name + ext)
    return "images/" + name + ext


def build(html, localize):
    parser = DocParser()
    parser.feed(html)
    content = {"title": "", "subtitle": "", "avatar": None, "intro": [], "sections": []}
    section = None
    for blk in parser.blocks:
        runs = tidy(blk["runs"])
        text = plain(runs)
        kind = blk["kind"]
        if kind == "title":
            content["title"] = text or content["title"]
        elif kind == "subtitle":
            content["subtitle"] = text
        elif kind == "h1":
            if text:
                section = {"title": text, "blocks": []}
                content["sections"].append(section)
        else:
            for img in blk["images"]:
                src = localize(img["src"])
                if section is None:
                    content["avatar"] = content["avatar"] or {"src": src, "alt": img["alt"] or text}
                else:
                    section["blocks"].append({"type": "note", "src": src, "alt": img["alt"], "caption": text})
            if text and not blk["images"]:
                if section is None:
                    content["intro"].append(runs)
                else:
                    section["blocks"].append({"type": "p", "runs": runs})
    return content


def main():
    with open(os.path.join(ROOT, "site.config.json")) as f:
        doc_id = json.load(f).get("docId", "")
    if not doc_id or doc_id.startswith("PASTE_"):
        print("site.config.json has no docId yet - nothing to sync.")
        return 0
    url = "https://docs.google.com/document/d/%s/export?format=html" % doc_id
    try:
        raw, _ = fetch(url)
    except Exception as e:
        print("Could not fetch the doc (is it shared as 'Anyone with the link can view'?):", e)
        return 1
    used = set()
    content = build(raw.decode("utf-8", "replace"), lambda u: save_image(u, used))
    if not content["sections"]:
        print("Doc parsed but has no Heading 1 sections - leaving content.json untouched.")
        return 1
    for fn in os.listdir(os.path.join(ROOT, "images")) if os.path.isdir(os.path.join(ROOT, "images")) else []:
        if fn not in used and fn != ".gitkeep":
            os.remove(os.path.join(ROOT, "images", fn))
    out = json.dumps(content, indent=2, ensure_ascii=False) + "\n"
    path = os.path.join(ROOT, "content.json")
    if not os.path.exists(path) or open(path, encoding="utf-8").read() != out:
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
        print("content.json updated.")
    else:
        print("No changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
