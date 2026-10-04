"""Turn attachments and links into article blocks.

- PDFs, Word documents (.docx), text and Markdown files, and HTML files
  attached to an email (or shared from the app) become readable blocks.
- An email that's just a link gets the web page fetched and turned into an
  article the same way newsletters are. A link to a PDF is read as a PDF.

Everything becomes the same plain blocks as email text (see article.py), so
the editor, the reader and the voice treat it all the same way.
"""

import io
import re
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

import article
from article import Block, Run

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 150
MAX_BLOCKS = 1500
USER_AGENT = "Mozilla/5.0 (compatible; ReadMe/1.0; personal newsletter reader)"

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SUPPORTED = {
    ".pdf": "application/pdf",
    ".docx": DOCX,
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
}

_URL = re.compile(r"https?://[^\s<>\"')\]]+")


class DocumentError(Exception):
    pass


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


def kind_of(filename: str, content_type: str = "") -> str | None:
    """The supported type of a file, or None."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in (filename or "") else ""
    if ext in SUPPORTED:
        return SUPPORTED[ext]
    content_type = (content_type or "").lower()
    if content_type in SUPPORTED.values():
        return content_type
    return None


def title_from_filename(filename: str) -> str:
    stem = (filename or "").rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    return re.sub(r"[_]+|(?<=\w)-(?=\w)", " ", stem).strip() or "Document"


def file_blocks(attachment: Attachment) -> list[Block]:
    kind = kind_of(attachment.filename, attachment.content_type)
    data = attachment.data
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError(f"{attachment.filename} is too big to read (over 20 MB).")
    if kind == "application/pdf" or data[:5] == b"%PDF-":
        return pdf_blocks(data)
    if kind == DOCX:
        return docx_blocks(data)
    if kind == "text/html":
        return article.extract(_decode(data))
    if kind in ("text/plain", "text/markdown"):
        return text_blocks(_decode(data))
    raise DocumentError(f"{attachment.filename or 'That file'} isn't a type Read Me can read.")


# --- PDF ----------------------------------------------------------------------

def pdf_blocks(data: bytes) -> list[Block]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentError("This PDF is password-protected, so it can't be read.")
        pages = [(page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES]]
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"This PDF couldn't be opened ({exc.__class__.__name__}).") from exc
    if not any(p.strip() for p in pages):
        raise DocumentError("This PDF is a scan (pictures of pages), so there's no text in it to read.")
    return reflow(_without_page_furniture(pages))


def _without_page_furniture(pages: list[str]) -> list[str]:
    """Lines of all pages in order, minus page numbers and the headers or
    footers that repeat on most pages."""
    per_page = [[line.strip() for line in p.splitlines()] for p in pages]
    repeated = set()
    if len(per_page) >= 3:
        counts = Counter(line for lines in per_page for line in set(lines) if line)
        repeated = {line for line, n in counts.items() if n > len(per_page) / 2 and len(line) < 120}
    lines: list[str] = []
    for page in per_page:
        for line in page:
            if line in repeated or re.fullmatch(r"(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?", line, re.I):
                continue
            lines.append(line)
        lines.append("")  # page break
    return lines


def reflow(lines: list[str]) -> list[Block]:
    """Rebuild paragraphs and headings from a PDF's line-by-line text."""
    lengths = sorted(len(line) for line in lines if line)
    if not lengths:
        return []
    full = lengths[max(0, int(len(lengths) * 0.9) - 1)]  # a typical full-width line
    blocks: list[Block] = []
    para = ""
    last_len = 0

    def flush():
        nonlocal para
        if para.strip():
            blocks.append(Block("p", [Run(para.strip())]))
        para = ""

    ends_sentence = re.compile(r"[.!?:\"”’)]$")
    for i, line in enumerate(lines):
        if not line:
            if ends_sentence.search(para):
                flush()  # a page break at the end of a sentence
            continue
        following = next((x for x in lines[i + 1:] if x), "")
        heading = (len(line) <= 80 and len(line) < 0.6 * full and not re.search(r"[.!?,;:]$", line)
                   and (not para or ends_sentence.search(para))
                   and following and not following[0].islower())
        if heading:
            flush()
            blocks.append(Block("h3", [Run(line)]))
            continue
        if para and ends_sentence.search(para) and last_len < 0.85 * full:
            flush()  # the previous line was the short last line of a paragraph
        if para.endswith("-") and line[:1].islower():
            para = para[:-1] + line  # a word hyphenated across lines
        else:
            para = f"{para} {line}" if para else line
        last_len = len(line)
    flush()
    return blocks[:MAX_BLOCKS]


# --- Word ---------------------------------------------------------------------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def docx_blocks(data: bytes) -> list[Block]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > 50 * 1024 * 1024:
                raise DocumentError("This Word document is too big to read.")
            root = ET.fromstring(z.read(info))
            links = _docx_links(z)
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("This Word document couldn't be opened.") from exc

    blocks: list[Block] = []
    for p in root.iter(W + "p"):
        props = p.find(W + "pPr")
        style = ""
        if props is not None and props.find(W + "pStyle") is not None:
            style = props.find(W + "pStyle").get(W + "val", "").lower()
        runs: list[Run] = []
        for child in p:
            if child.tag == W + "hyperlink":
                href = links.get(child.get(R_ID, ""), "")
                runs += [_docx_run(r, href) for r in child.iter(W + "r")]
            elif child.tag == W + "r":
                runs.append(_docx_run(child))
        runs = [r for r in runs if r.text]
        if not "".join(r.text for r in runs).strip():
            continue
        if style.startswith("heading1") or style == "title":
            kind = "h2"
        elif style.startswith("heading"):
            kind = "h3"
        elif props is not None and props.find(W + "numPr") is not None or "list" in style:
            kind = "li"
        elif "quote" in style:
            kind = "quote"
        else:
            kind = "p"
        blocks.append(Block(kind, runs))
    return blocks[:MAX_BLOCKS]


def _docx_run(r, href: str = "") -> Run:
    text = ""
    for node in r:
        if node.tag == W + "t":
            text += node.text or ""
        elif node.tag in (W + "br", W + "cr"):
            text += "\n"
        elif node.tag == W + "tab":
            text += " "
    props = r.find(W + "rPr")

    def on(tag):
        el = props.find(W + tag) if props is not None else None
        return el is not None and el.get(W + "val", "true") not in ("0", "false")

    safe = href if href.lower().startswith(("http://", "https://", "mailto:")) else ""
    return Run(text, safe, on("b"), on("i"))


def _docx_links(z: zipfile.ZipFile) -> dict[str, str]:
    try:
        rels = ET.fromstring(z.read("word/_rels/document.xml.rels"))
    except KeyError:
        return {}
    return {r.get("Id"): r.get("Target", "") for r in rels if r.get("TargetMode") == "External"}


# --- text and Markdown --------------------------------------------------------

def text_blocks(text: str) -> list[Block]:
    blocks = []
    for block in article.extract("", text):
        heading = re.match(r"^(#{1,6})\s+(.+)$", block.text)
        if heading and "\n" not in block.text:
            blocks.append(Block("h2" if len(heading.group(1)) <= 2 else "h3", [Run(heading.group(2))]))
        else:
            blocks.append(block)
    return blocks[:MAX_BLOCKS]


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


# --- links --------------------------------------------------------------------

def lone_link(text: str, subject: str = "") -> str | None:
    """The link, if an email or share is basically just one link (maybe with
    a few words like "check this out")."""
    urls = list(dict.fromkeys(_URL.findall(text or "") + _URL.findall(subject or "")))
    if len(urls) != 1:
        return None
    words = _URL.sub(" ", text or "").split()
    return urls[0].rstrip(".,;") if len(words) <= 25 else None


@dataclass
class Page:
    title: str
    site: str
    blocks: list[Block]


def fetch_page(url: str, opener=None) -> Page:
    """Download a web page (or a linked PDF) and turn it into blocks."""
    opener = opener or _open
    try:
        final_url, content_type, data = opener(url)
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"Couldn't open that link ({exc.__class__.__name__}).") from exc
    site = urlparse(final_url).netloc.removeprefix("www.")
    if content_type == "application/pdf" or data[:5] == b"%PDF-":
        name = urlparse(final_url).path.rsplit("/", 1)[-1]
        return Page(title_from_filename(name) if name else site, site, pdf_blocks(data))
    if content_type in ("text/plain", "text/markdown"):
        return Page(site, site, text_blocks(_decode(data)))

    soup = BeautifulSoup(_decode(data), "html.parser")
    title = _meta(soup, "og:title") or (soup.title.get_text(" ", strip=True) if soup.title else "") or site
    site = _meta(soup, "og:site_name") or site
    # Prefer the article itself over the whole page with its menus and sidebars.
    candidates = soup.find_all("article") or soup.find_all("main") or [soup.body or soup]
    main = max(candidates, key=lambda el: len(el.get_text(" ", strip=True)))
    blocks = article.extract(str(main), base_url=final_url)
    image = _meta(soup, "og:image")
    if image and not any(b.kind == "img" for b in blocks[:3]):
        blocks.insert(0, Block("img", src=urljoin(final_url, image), alt=title))
    if not any(article.spoken_text(b) for b in blocks):
        raise DocumentError("That page didn't have any readable text (it may need a login).")
    return Page(title, site, blocks[:MAX_BLOCKS])


def _meta(soup, prop: str) -> str:
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return (tag.get("content") or "").strip() if tag else ""


def _open(url: str) -> tuple[str, str, bytes]:
    if not url.lower().startswith(("http://", "https://")):
        raise DocumentError("Only web links can be opened.")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,*/*"})
    with urllib.request.urlopen(request, timeout=25) as response:
        data = response.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise DocumentError("That page is too big to read.")
        return response.geturl(), response.headers.get_content_type(), data
