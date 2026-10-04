import io
import zipfile
from pathlib import Path

import pytest

import documents
from documents import Attachment

PDF = (Path(__file__).parent / "fixtures" / "notice.pdf").read_bytes()


def test_pdf_rebuilds_headings_and_paragraphs():
    blocks = documents.pdf_blocks(PDF)
    assert [b.kind for b in blocks] == ["h3", "p", "p", "h3", "p", "p"]
    assert blocks[0].text == "Irrigation District Notice"
    assert blocks[1].text.startswith("Water deliveries for the 2027 season will begin on April 15.")
    assert blocks[4].text == "The standby charge remains $18.50 per acre. Late orders will be filled as capacity allows."


def test_pdf_found_by_content_even_with_odd_name():
    blocks = documents.file_blocks(Attachment("scan0001", "application/octet-stream", PDF))
    assert blocks[0].text == "Irrigation District Notice"


def test_bad_pdf_and_too_big_file_explain_themselves():
    with pytest.raises(documents.DocumentError, match="couldn't be opened"):
        documents.pdf_blocks(b"%PDF-1.4 not really")
    big = Attachment("big.pdf", "application/pdf", b"x" * (documents.MAX_FILE_BYTES + 1))
    with pytest.raises(documents.DocumentError, match="too big"):
        documents.file_blocks(big)
    with pytest.raises(documents.DocumentError, match="isn't a type"):
        documents.file_blocks(Attachment("photo.jpg", "image/jpeg", b"..."))


def test_page_numbers_and_repeated_headers_are_dropped():
    pages = [f"Acme Co. Newsletter\nStory text on page {n}.\nPage {n} of 3" for n in (1, 2, 3)]
    lines = documents._without_page_furniture(pages)
    assert [x for x in lines if x] == ["Story text on page 1.", "Story text on page 2.", "Story text on page 3."]


def test_reflow_joins_hyphenated_words_and_wrapped_lines():
    line = "This line is long enough to count as a full line of text in a PDF, for-"
    blocks = documents.reflow([line, "mat reasons it keeps going here and ends here."])
    assert len(blocks) == 1 and "for-mat" not in blocks[0].text and "format reasons" in blocks[0].text


def _docx():
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' \
         'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    doc = f"""<w:document {ns}><w:body>
      <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Spray plan</w:t></w:r></w:p>
      <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Start</w:t></w:r><w:r><w:t xml:space="preserve"> at dawn. </w:t></w:r>
        <w:hyperlink r:id="rId9"><w:r><w:t>Label</w:t></w:r></w:hyperlink></w:p>
      <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr><w:r><w:t>Check nozzles</w:t></w:r></w:p>
      <w:p><w:r><w:t></w:t></w:r></w:p>
    </w:body></w:document>"""
    rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId9" Type="hyperlink" Target="https://example.com/label" TargetMode="External"/>
    </Relationships>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", rels)
    return buf.getvalue()


def test_word_document_keeps_headings_lists_bold_and_links():
    blocks = documents.file_blocks(Attachment("Plan.docx", "", _docx()))
    assert [(b.kind, b.text) for b in blocks] == [
        ("h2", "Spray plan"), ("p", "Start at dawn. Label"), ("li", "Check nozzles")]
    assert blocks[1].runs[0].bold and blocks[1].runs[-1].href == "https://example.com/label"
    with pytest.raises(documents.DocumentError):
        documents.docx_blocks(b"not a zip")


def test_text_and_markdown():
    blocks = documents.file_blocks(Attachment("notes.md", "", b"# Harvest\n\nWe start Monday.\n\n### Crew\n\nSix people."))
    assert [(b.kind, b.text) for b in blocks] == [
        ("h2", "Harvest"), ("p", "We start Monday."), ("h3", "Crew"), ("p", "Six people.")]
    assert documents.file_blocks(Attachment("a.txt", "", "Caf\xe9 hours".encode("cp1252")))[0].text == "Caf\xe9 hours"


def test_kind_and_title_from_filename():
    assert documents.kind_of("Report.PDF") == "application/pdf"
    assert documents.kind_of("file", "text/plain") == "text/plain"
    assert documents.kind_of("photo.jpg", "image/jpeg") is None
    assert documents.title_from_filename("2027_water-notice.pdf") == "2027 water notice"


def test_lone_link():
    assert documents.lone_link("Check this out https://example.com/story.") == "https://example.com/story"
    assert documents.lone_link("", "https://example.com/a") == "https://example.com/a"
    assert documents.lone_link("https://a.com and https://b.com") is None
    assert documents.lone_link("word " * 30 + "https://a.com") is None
    assert documents.lone_link("no links here") is None


PAGE = b"""<html><head><title>Ignored</title>
<meta property="og:title" content="Rain on the way">
<meta property="og:site_name" content="Valley News">
<meta property="og:image" content="/img/rain.jpg"></head>
<body><nav><a href="/">Home</a> <a href="/sports">Sports</a></nav>
<article><h1>Rain on the way</h1><p>A storm reaches the valley Tuesday. <a href="/radar">Radar</a></p></article>
<aside>Subscribe now for more stories like this one.</aside></body></html>"""


def test_fetch_page_reads_the_article_not_the_menus():
    page = documents.fetch_page("https://news.example.com/s", opener=lambda url: (
        "https://www.news.example.com/2026/rain", "text/html", PAGE))
    assert (page.title, page.site) == ("Rain on the way", "Valley News")
    assert page.blocks[0].kind == "img" and page.blocks[0].src == "https://www.news.example.com/img/rain.jpg"
    texts = [b.text for b in page.blocks]
    assert "A storm reaches the valley Tuesday. Radar" in texts
    assert not any("Sports" in t or "Subscribe" in t for t in texts)
    link = [b for b in page.blocks if b.kind == "p"][0].runs[-1]
    assert link.href == "https://www.news.example.com/radar"


def test_fetch_page_reads_a_linked_pdf():
    page = documents.fetch_page("https://d.example.org/files/water_notice.pdf",
                                opener=lambda url: (url, "application/octet-stream", PDF))
    assert page.title == "water notice" and page.site == "d.example.org"
    assert page.blocks[0].text == "Irrigation District Notice"


def test_fetch_page_errors_are_explained():
    def broken(url):
        raise OSError("boom")

    with pytest.raises(documents.DocumentError, match="Couldn't open that link"):
        documents.fetch_page("https://x.com", opener=broken)
    with pytest.raises(documents.DocumentError, match="readable text"):
        documents.fetch_page("https://x.com", opener=lambda u: (u, "text/html", b"<html><body></body></html>"))
    with pytest.raises(documents.DocumentError, match="Only web links"):
        documents._open("file:///etc/passwd")


def test_scanned_pdf_says_it_has_no_text():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(documents.DocumentError, match="scan"):
        documents.pdf_blocks(buf.getvalue())
