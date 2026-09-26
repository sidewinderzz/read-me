"""Turn an email into an article: a list of clean blocks (headings, paragraphs,
list items, quotes, images) with the original wording, links and pictures.

Newsletter HTML is a maze of layout tables, hidden preview text and tracking
pixels. This walks it once and keeps only what a reader would see. Nothing
here trusts the email: text is kept as plain strings and links as checked
URLs, so the player page can build the article without ever inserting the
email's own HTML.
"""

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

BLOCK_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "table", "tbody",
    "thead", "tfoot", "tr", "td", "th", "blockquote", "section", "article", "header",
    "footer", "center", "hr", "pre", "figure", "figcaption", "main", "aside", "nav",
    "dl", "dt", "dd", "form",
}
SKIP_TAGS = {"script", "style", "head", "title", "meta", "link", "noscript", "svg", "button",
             "input", "select", "textarea", "iframe", "object", "video", "audio", "map"}
_HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|max-height\s*:\s*0|mso-hide\s*:\s*all"
                     r"|font-size\s*:\s*0|opacity\s*:\s*0(?![.\d])", re.I)
_TRACKER = re.compile(r"pixel|track|beacon|spacer|/open\b|/o/|analytics", re.I)
_INVISIBLE = re.compile("[​‌‍⁠﻿͏­]")
_URL = re.compile(r"https?://[^\s<>\"')]+|www\.[^\s<>\"')]+")


@dataclass
class Run:
    text: str
    href: str = ""
    bold: bool = False
    italic: bool = False


@dataclass
class Block:
    kind: str  # "h2", "h3", "p", "li", "quote", "img" or "row" (a row of a data table)
    runs: list[Run] = field(default_factory=list)
    src: str = ""  # images only
    alt: str = ""
    href: str = ""  # an image that links somewhere
    cells: list[str] = field(default_factory=list)  # rows only
    # Set by the editor (Claude):
    quiet: bool = False  # shown, but not read aloud (bylines, photo credits, small print)
    say: str = ""  # read aloud instead of this block and the rest of its group (e.g. a table)
    group: int = -1  # blocks that share a spoken sentence share this number

    @property
    def text(self) -> str:
        if self.kind == "row":
            return " | ".join(self.cells)
        return "".join(r.text for r in self.runs).strip()


def extract(html: str | None, plain: str = "") -> list[Block]:
    """Blocks in reading order. Falls back to the plain-text part when the
    email has no HTML."""
    if html and html.strip():
        blocks = _from_html(html)
        if any(b.text for b in blocks):
            return blocks
    return _from_plain(plain)


def spoken_text(block: Block) -> str:
    """What the voice says for a block: its words, without raw web addresses.
    The editor can replace a group of blocks with one sentence, or keep a
    block on screen but out of the audio."""
    if block.say:
        return block.say.strip()
    if block.kind == "img" or block.quiet or block.group >= 0:
        return ""
    text = " ".join(block.cells) if block.kind == "row" else block.text
    text = _URL.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if re.search(r"[A-Za-z0-9]", text) else ""


def outline(blocks: list[Block], limit: int = 240) -> str:
    """A numbered, shortened list of the blocks for Claude to judge."""
    lines = []
    for i, b in enumerate(blocks):
        if b.kind == "img":
            desc = f"[image{': ' + b.alt[:80] if b.alt else ''}{' (linked)' if b.href else ''}]"
        else:
            desc = re.sub(r"\s+", " ", b.text)
            if len(desc) > limit:
                desc = desc[:limit] + "…"
            links = sum(1 for r in b.runs if r.href)
            if links:
                desc += f"  ({links} link{'s' if links > 1 else ''})"
        lines.append(f"{i} [{b.kind}] {desc}")
    return "\n".join(lines)


def to_json(blocks: list[Block]) -> list[dict]:
    out = []
    for b in blocks:
        if b.kind == "img":
            item = {"kind": "img", "src": b.src, "alt": b.alt, "href": b.href}
        elif b.kind == "row":
            item = {"kind": "row", "cells": list(b.cells)}
        else:
            runs = []
            for r in b.runs:
                run = {"text": r.text}
                if r.href:
                    run["href"] = r.href
                if r.bold:
                    run["bold"] = True
                if r.italic:
                    run["italic"] = True
                runs.append(run)
            item = {"kind": b.kind, "runs": runs}
        if b.quiet:
            item["quiet"] = True
        if b.say:
            item["say"] = b.say
        if b.group >= 0:
            item["g"] = b.group
        out.append(item)
    return out


# --- HTML -------------------------------------------------------------------

class _Walker:
    def __init__(self):
        self.blocks: list[Block] = []
        self.runs: list[Run] = []
        self.kind = "p"

    def walk(self, node: Tag, href="", bold=False, italic=False, kind="p"):
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                text = _INVISIBLE.sub("", str(child))
                if text:
                    self.kind = kind
                    self.runs.append(Run(text, href, bold, italic))
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            if name in SKIP_TAGS or _HIDDEN.search(child.get("style", "") or "") or child.has_attr("hidden"):
                continue
            if name == "tr" and self.data_row(child):
                continue
            if name == "br":
                self.runs.append(Run("\n", href, bold, italic))
            elif name == "img":
                self.image(child, href)
            elif name in BLOCK_TAGS:
                self.flush()
                self.walk(child, href, bold, italic, _block_kind(name, kind))
                self.flush()
            elif name == "a":
                link = _safe_href(child.get("href", ""))
                self.walk(child, link or href, bold, italic, kind)
            elif name in ("b", "strong") or _is_bold(child):
                self.walk(child, href, True, italic, kind)
            elif name in ("i", "em"):
                self.walk(child, href, bold, True, kind)
            else:
                self.walk(child, href, bold, italic, kind)

    def data_row(self, tr: Tag) -> bool:
        """A table row of short cells with numbers in it (a market ticker,
        scores, prices) becomes one row block instead of a line per cell."""
        cells = [c for c in tr.find_all(["td", "th"], recursive=False)
                 if not _HIDDEN.search(c.get("style", "") or "")]
        texts = [re.sub(r"\s+", " ", _INVISIBLE.sub("", c.get_text(" "))).strip() for c in cells]
        filled = [t for t in texts if t]
        numeric = sum(1 for t in filled if re.search(r"\d", t))
        if len(filled) < 2 or any(len(t) > 40 for t in filled) or numeric < 2:
            return False
        self.flush()
        self.blocks.append(Block("row", cells=filled))
        return True

    def image(self, tag: Tag, href: str):
        src = (tag.get("src") or "").strip()
        if not src.startswith(("https://", "http://")) or _TRACKER.search(src):
            return
        width, height = _size(tag.get("width")), _size(tag.get("height"))
        style = tag.get("style", "") or ""
        width = width or _size(_css(style, "width"))
        height = height or _size(_css(style, "height"))
        if (width and width < 48) or (height and height < 48):
            return  # spacers, icons, social buttons
        self.flush()
        alt = re.sub(r"\s+", " ", _INVISIBLE.sub("", tag.get("alt") or "")).strip()
        self.blocks.append(Block("img", src=src, alt=alt[:200], href=href))

    def flush(self):
        runs = _tidy(self.runs)
        self.runs = []
        if not runs:
            return
        kind = self.kind
        text = "".join(r.text for r in runs).strip()
        # Many newsletters style headings with bold text instead of <h> tags.
        if kind == "p" and len(text) <= 90 and all(r.bold for r in runs if r.text.strip()) \
                and not text.endswith((".", ",", ":", ";")) and "\n" not in text:
            kind = "h3"
        if self.blocks and self.blocks[-1].kind == kind and self.blocks[-1].text == text:
            return  # duplicated text (common in mobile/desktop variants)
        self.blocks.append(Block(kind, runs))


def _from_html(html: str) -> list[Block]:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.body or soup
    walker = _Walker()
    walker.walk(root)
    walker.flush()
    return walker.blocks


def _block_kind(tag: str, current: str) -> str:
    if tag in ("h1", "h2"):
        return "h2"
    if tag in ("h3", "h4", "h5", "h6"):
        return "h3"
    if tag == "li":
        return "li"
    if tag == "blockquote":
        return "quote"
    if current in ("li", "quote"):
        return current
    return "p"


def _tidy(runs: list[Run]) -> list[Run]:
    """Collapse whitespace, trim the edges and merge neighbouring runs that
    share formatting."""
    out: list[Run] = []
    for r in runs:
        text = re.sub(r"[ \t\r\f\v\xa0]+", " ", r.text)
        text = re.sub(r" *\n *", "\n", text)
        if out and (out[-1].href, out[-1].bold, out[-1].italic) == (r.href, r.bold, r.italic):
            out[-1].text += text
        else:
            out.append(Run(text, r.href, r.bold, r.italic))
    # trim leading/trailing whitespace across the whole block
    while out and not out[0].text.strip():
        out.pop(0)
    while out and not out[-1].text.strip():
        out.pop()
    if not out:
        return []
    out[0].text = out[0].text.lstrip()
    out[-1].text = out[-1].text.rstrip()
    for r in out:
        r.text = re.sub(r"\n{3,}", "\n\n", r.text)
        if not r.text.strip():
            r.bold = r.italic = False  # formatting on bare spaces is meaningless
    return out if any(r.text.strip() for r in out) else []


def _safe_href(href: str) -> str:
    href = (href or "").strip()
    return href if href.lower().startswith(("https://", "http://", "mailto:")) else ""


def _is_bold(tag: Tag) -> bool:
    style = (tag.get("style", "") or "").lower()
    match = re.search(r"font-weight\s*:\s*(\w+)", style)
    return bool(match) and (match.group(1) in ("bold", "bolder") or match.group(1).isdigit() and int(match.group(1)) >= 600)


def _css(style: str, prop: str) -> str:
    match = re.search(rf"(?:^|;)\s*{prop}\s*:\s*([^;]+)", style, re.I)
    return match.group(1) if match else ""


def _size(value) -> int:
    match = re.match(r"\s*(\d+)", str(value or ""))
    return int(match.group(1)) if match else 0


# --- plain text -------------------------------------------------------------

def _from_plain(text: str) -> list[Block]:
    blocks = []
    for para in re.split(r"\n\s*\n", _INVISIBLE.sub("", text or "")):
        para = para.strip()
        if not para:
            continue
        runs, pos = [], 0
        for m in _URL.finditer(para):
            if m.start() > pos:
                runs.append(Run(para[pos:m.start()]))
            url = m.group(0)
            runs.append(Run(url, href=url if url.startswith("http") else "https://" + url))
            pos = m.end()
        if pos < len(para):
            runs.append(Run(para[pos:]))
        blocks.append(Block("p", runs))
    return blocks



# --- the article file (text/<id>.json) ----------------------------------------

def from_json(data: list[dict]) -> list[Block]:
    blocks = []
    for b in data or []:
        if b.get("kind") == "img":
            block = Block("img", src=b.get("src", ""), alt=b.get("alt", ""), href=b.get("href", ""))
        elif b.get("kind") == "row":
            block = Block("row", cells=[str(c) for c in b.get("cells", [])])
        else:
            runs = [Run(r.get("text", ""), r.get("href", ""), bool(r.get("bold")), bool(r.get("italic")))
                    for r in b.get("runs", [])]
            block = Block(b.get("kind", "p"), runs)
        block.quiet = bool(b.get("quiet"))
        block.say = str(b.get("say") or "")
        block.group = int(b.get("g", -1))
        blocks.append(block)
    return blocks


def reading_doc(intro: str, blocks: list[Block]) -> dict:
    """The article as saved for the app. Timings are added once it's voiced."""
    return {"version": 3, "intro": intro, "blocks": to_json(blocks)}


def segments(doc: dict) -> tuple[list[str], list[int]]:
    """What the voice says, in order: the intro, then every block with words.
    Also returns which block each spoken segment (after the intro) belongs to."""
    said, owners = [doc.get("intro") or ""], []
    for i, block in enumerate(from_json(doc.get("blocks", []))):
        words = spoken_text(block)
        if words:
            said.append(words)
            owners.append(i)
    return said, owners


def add_timings(doc: dict, owners: list[int], seconds: list[float]) -> dict:
    """Mark where each spoken block starts in the audio, for the read-along."""
    starts, t = [], 0.0
    for s in seconds:
        starts.append(round(t, 2))
        t += s
    for n, i in enumerate(owners):
        doc["blocks"][i]["t"] = starts[n + 1]  # starts[0] is the intro
    doc["total"] = round(t, 2)
    return doc
