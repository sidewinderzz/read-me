import xml.etree.ElementTree as ET

import podcast
from podcast import Episode, build_feed, build_player


def _ep(i, title="Corn & beans <update>"):
    return Episode(id=f"id{i}", title=title, sender="Farm Journal", published=f"2026-09-2{i}T12:00:00+00:00",
                   audio=f"audio/id{i}.mp3", bytes=1234, chars=5000)


def test_feed_is_valid_rss_newest_first(tmp_path):
    from conftest import FakeStore
    xml = build_feed([_ep(1), _ep(3)], "Read Aloud", FakeStore())
    root = ET.fromstring(xml)
    items = root.findall("./channel/item")
    assert [i.findtext("guid") for i in items] == ["id3", "id1"]
    assert items[0].findtext("title") == "Corn & beans <update>"
    enc = items[0].find("enclosure")
    assert enc.get("url").endswith("/secret/audio/id3.mp3")
    assert enc.get("type") == "audio/mpeg"
    assert root.find(f"./channel/{{{podcast.ITUNES}}}block").text == "yes"


def test_player_escapes_script_breakers():
    from conftest import FakeStore
    html = build_player([_ep(1, title="</script><script>alert(1)</script>")], "Read Aloud",
                        "https://x/feed.xml", FakeStore())
    assert "</script><script>alert(1)" not in html
    assert "__EPISODES_JSON__" not in html and "__TITLE_JSON__" not in html


def test_player_links_text_when_present():
    from conftest import FakeStore
    ep = _ep(1)
    ep.text = "text/id1.txt"
    html = build_player([ep, _ep(2)], "Read Aloud", "https://x/feed.xml", FakeStore())
    assert '"textUrl": "https://storage.googleapis.com/bucket/secret/text/id1.txt"' in html
    assert '"textUrl": ""' in html  # older episodes without text still work


def test_app_files_uploaded_once_and_manifest_valid():
    import json
    from conftest import FakeStore
    store, state = FakeStore(), {"episodes": [], "processed": {}, "usage": {}}
    podcast.publish(store, state, "Read Aloud")
    manifest = json.loads(store.files["manifest.webmanifest"])
    assert manifest["display"] == "standalone" and manifest["start_url"] == "./index.html"
    for icon in manifest["icons"]:
        assert icon["src"] in store.files
    assert "sw.js" in store.files
    assert not podcast.assets_outdated(state, "Read Aloud")

    writes = []
    store.write = lambda name, *a, **k: writes.append(name)
    podcast.publish(store, state, "Read Aloud")
    assert "sw.js" not in writes and "manifest.webmanifest" not in writes
    assert podcast.assets_outdated(state, "New Title")
