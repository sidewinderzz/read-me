"""Build and publish the Read Me app page and the podcast feed.

Everything lives under a secret folder name (FEED_SECRET) inside the bucket:

    <secret>/index.html    the Read Me app
    <secret>/feed.xml      the podcast feed (only emails that have audio)
    <secret>/text/*.json   each email as an article: blocks, links, photos, timings
    <secret>/audio/*.mp3   audio, for the emails that have been voiced
    <secret>/manifest.webmanifest, sw.js, icons/   make the app installable
    (state.json, settings.json and voiced/ are described in store.py)

The bucket lets anyone fetch a file whose exact address they know, but not list
what's in it, so the random folder name is what keeps your emails private.
"""

import email.utils
import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import store as storage

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", ITUNES)

HERE = Path(__file__).parent
PLAYER_TEMPLATE = (HERE / "player.html").read_text()
ICONS = ["icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"]


@dataclass
class Episode:
    id: str
    title: str
    sender: str
    published: str  # ISO 8601
    audio: str  # path inside the feed folder, e.g. audio/ab12.mp3; "" until voiced
    bytes: int  # size of the MP3 (0 until voiced)
    chars: int  # characters the voice reads (or would read)
    text: str = ""  # path to the article, e.g. text/ab12.json
    sender_address: str = ""


def load_state(store) -> dict:
    state = store.read_json("state.json") or {}
    state.setdefault("episodes", [])
    state.setdefault("processed", {})
    state.setdefault("usage", {})
    state.setdefault("senders", {})
    return state


def publish(store, state: dict, title: str) -> None:
    """Write state, feed and player page. Called after every new episode so a
    crash halfway through a run never loses finished work."""
    episodes = [Episode(**e) for e in state["episodes"]]
    feed_url = store.url("feed.xml")
    extras = {
        "senders": [{"address": a, "name": s.get("name", a)} for a, s in state.get("senders", {}).items()],
        "always": storage.always_voice(store),
        "limit": storage.monthly_limit(),
        "folder": getattr(store, "folder", ""),
    }
    store.write("state.json", json.dumps(state, indent=1), "application/json")
    store.write("feed.xml", build_feed(episodes, title, store), "application/rss+xml; charset=utf-8")
    store.write("index.html", build_player(episodes, title, feed_url, store, extras), "text/html; charset=utf-8")
    if assets_outdated(state, title):
        _upload_app_files(store, title)
        state["app_files"] = _app_files_version(title)
        store.write("state.json", json.dumps(state, indent=1), "application/json")


def assets_outdated(state: dict, title: str) -> bool:
    """True when the app page, manifest, service worker or icons changed since
    they were last published (or never were)."""
    return state.get("app_files") != _app_files_version(title)


def build_manifest(title: str) -> str:
    # Android takes the installed app's system bar and splash colours from
    # here. They're the dark "ink" colour; in light mode the page's own
    # theme-color tag switches the top bar back to paper once it loads.
    ink = "#111110"
    return json.dumps({
        "name": title,
        "short_name": title[:12],
        "id": "./",
        "start_url": "./index.html",
        "scope": "./",
        "display": "standalone",
        "background_color": ink,
        "theme_color": ink,
        "icons": [
            {"src": "icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, indent=1)


def _upload_app_files(store, title: str) -> None:
    store.write("manifest.webmanifest", build_manifest(title), "application/manifest+json")
    store.write("sw.js", (HERE / "sw.js").read_text(), "text/javascript; charset=utf-8")
    for name in ICONS:
        store.write(f"icons/{name}", (HERE / "icons" / name).read_bytes(), "image/png", cache=True)


def _app_files_version(title: str) -> str:
    # Includes the app page itself, so a change to the app gets published on
    # the next run even when there's no new mail.
    digest = hashlib.sha1(build_manifest(title).encode())
    digest.update(PLAYER_TEMPLATE.encode())
    digest.update((HERE / "sw.js").read_bytes())
    for name in ICONS:
        digest.update((HERE / "icons" / name).read_bytes())
    return digest.hexdigest()[:12]


def build_feed(episodes: list[Episode], title: str, store) -> str:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = store.url("index.html")
    ET.SubElement(channel, "description").text = "Your emails and newsletters, out loud."
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, f"{{{ITUNES}}}author").text = title
    ET.SubElement(channel, f"{{{ITUNES}}}explicit").text = "false"
    # Keeps the feed out of Apple's public podcast directory.
    ET.SubElement(channel, f"{{{ITUNES}}}block").text = "yes"

    for ep in sorted(episodes, key=lambda e: e.published, reverse=True):
        if not ep.audio:
            continue  # not voiced (yet): podcast apps can only play what exists
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = ep.title
        ET.SubElement(item, "description").text = f"From {ep.sender}"
        ET.SubElement(item, f"{{{ITUNES}}}author").text = ep.sender
        ET.SubElement(item, "pubDate").text = email.utils.format_datetime(datetime.fromisoformat(ep.published))
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = ep.id
        ET.SubElement(
            item,
            "enclosure",
            {"url": store.url(ep.audio), "length": str(ep.bytes), "type": "audio/mpeg"},
        )

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode")


def build_player(episodes: list[Episode], title: str, feed_url: str, store, extras: dict | None = None) -> str:
    data = [
        {**asdict(ep), "url": store.url(ep.audio) if ep.audio else "",
         "textUrl": store.url(ep.text) if ep.text else ""}
        for ep in sorted(episodes, key=lambda e: e.published, reverse=True)
    ]
    extras = extras or {"senders": [], "always": [], "limit": storage.MONTHLY_CHAR_LIMIT_DEFAULT, "folder": ""}
    # "<" is escaped so an email subject can never close the <script> tag early.
    as_json = lambda value: json.dumps(value).replace("<", "\\u003c")
    return (
        PLAYER_TEMPLATE.replace("__TITLE_JSON__", as_json(title))
        .replace("__FEED_URL_JSON__", as_json(feed_url))
        .replace("__EPISODES_JSON__", as_json(data))
        .replace("__EXTRAS_JSON__", as_json(extras))
    )
