import article

NEWSLETTER = """<html><head><style>.x{color:red}</style></head><body>
<div style="display:none;max-height:0">Preview text you never see &zwnj;&zwnj;</div>
<table><tr><td>
  <table><tr><td align="center"><a href="https://brew.com/web"><img src="https://img.brew.com/logo.png" width="200" alt="Morning Brew"></a></td></tr></table>
  <table><tr><td>
    <h1>Mortgage rates go up</h1>
    <p>Rates hit <b>7.1%</b> this week, the highest since spring.<br>Builders are <em>worried</em>.
       Read <a href="https://news.example.com/rates?utm=1">the report</a>.</p>
    <p><img src="https://img.example.com/chart.png" width="600" height="300" alt="Rate chart"></p>
    <p><span style="font-weight:700">Quick hits</span></p>
    <ul><li>Corn is up.</li><li>Beans are <a href="javascript:alert(1)">down</a>.</li></ul>
    <blockquote>"It's a buyer's market," said a broker.</blockquote>
    <img src="https://t.example.com/open/abc.gif" width="1" height="1">
    <img src="https://img.example.com/fb.png" width="24" height="24" alt="Facebook">
  </td></tr></table>
</td></tr></table>
</body></html>"""


def _blocks():
    return article.extract(NEWSLETTER)


def test_structure_and_order():
    blocks = _blocks()
    assert [b.kind for b in blocks] == ["img", "h2", "p", "img", "h3", "li", "li", "quote"]
    assert blocks[1].text == "Mortgage rates go up"
    assert blocks[4].text == "Quick hits"  # bold-styled line becomes a heading


def test_hidden_preview_text_trackers_and_icons_are_dropped():
    texts = " ".join(b.text for b in _blocks())
    assert "Preview text" not in texts and "color:red" not in texts
    srcs = [b.src for b in _blocks() if b.kind == "img"]
    assert srcs == ["https://img.brew.com/logo.png", "https://img.example.com/chart.png"]


def test_links_formatting_and_line_breaks_kept():
    p = _blocks()[2]
    assert p.text.startswith("Rates hit 7.1% this week")
    assert "\nBuilders" in p.text
    assert any(r.bold and r.text == "7.1%" for r in p.runs)
    assert any(r.italic and r.text == "worried" for r in p.runs)
    link = next(r for r in p.runs if r.href)
    assert (link.text, link.href) == ("the report", "https://news.example.com/rates?utm=1")
    assert _blocks()[0].href == "https://brew.com/web"  # a linked image keeps its link


def test_unsafe_links_are_dropped():
    li = _blocks()[6]
    assert li.text == "Beans are down."
    assert not any(r.href for r in li.runs)


def test_spoken_text_skips_images_and_urls():
    blocks = _blocks()
    assert article.spoken_text(blocks[0]) == ""
    assert article.spoken_text(article.Block("p", [article.Run("See https://x.com/a now")])) == "See now"


def test_outline_numbers_blocks():
    lines = article.outline(_blocks()).splitlines()
    assert lines[0] == "0 [img] [image: Morning Brew (linked)]"
    assert lines[2].startswith("2 [p] Rates hit 7.1%") and lines[2].endswith("(1 link)")


def test_json_has_no_html():
    data = article.to_json(_blocks())
    assert data[3] == {"kind": "img", "src": "https://img.example.com/chart.png", "alt": "Rate chart", "href": ""}
    assert all("<" not in r["text"] for b in data if b["kind"] != "img" for r in b["runs"])


def test_plain_text_fallback_links_urls():
    blocks = article.extract("", "Hi,\n\nSee https://example.com/x for details.")
    assert [b.text for b in blocks] == ["Hi,", "See https://example.com/x for details."]
    assert blocks[1].runs[1].href == "https://example.com/x"


def test_article_file_round_trip_segments_and_timings():
    blocks = article.extract(NEWSLETTER)
    doc = article.reading_doc("From Morning Brew. Rates.", blocks)
    assert article.to_json(article.from_json(doc["blocks"])) == doc["blocks"]
    said, owners = article.segments(doc)
    assert said[0] == "From Morning Brew. Rates." and said[1] == "Mortgage rates go up"
    assert owners[0] == 1 and 0 not in owners  # the logo image isn't spoken
    article.add_timings(doc, owners, [2.0] + [1.0] * len(owners))
    assert doc["blocks"][1]["t"] == 2.0 and doc["blocks"][2]["t"] == 3.0
    assert doc["total"] == 2.0 + len(owners)


# Morning Brew's ticker: an outer row per market, each holding a small table
# with an arrow image, the name, the value and the change.
TICKER = """<table><tr><td><table><tr>
  <td><img src="https://x/arrow.png" width="12" height="12"></td><td><p>Nasdaq</p></td>
  <td><p>27,068.72</p></td><td><table><tr><td>+0.48%</td></tr></table></td>
</tr></table></td></tr>
<tr><td><table><tr>
  <td><img src="https://x/arrow.png" width="12" height="12"></td><td><p>Bitcoin</p></td>
  <td><p>$83,989.57</p></td><td><table><tr><td>-0.44%</td></tr></table></td>
</tr></table></td></tr></table>
<table><tr><td>September 26, 2026</td><td><a href="https://x/web">View Online</a></td></tr></table>"""


def test_ticker_rows_become_table_rows_but_a_dateline_does_not():
    blocks = article.extract(TICKER)
    rows = [b.cells for b in blocks if b.kind == "row"]
    assert rows == [["Nasdaq", "27,068.72", "+0.48%"], ["Bitcoin", "$83,989.57", "-0.44%"]]
    assert [b.text for b in blocks if b.kind == "p"] == ["September 26, 2026", "View Online"]
    data = article.to_json(blocks)
    assert data[0] == {"kind": "row", "cells": ["Nasdaq", "27,068.72", "+0.48%"]}
    assert article.from_json(data)[0].cells == ["Nasdaq", "27,068.72", "+0.48%"]
    assert article.spoken_text(blocks[0]) == "Nasdaq 27,068.72 +0.48%"  # if no SAY sentence is given
