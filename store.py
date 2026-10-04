"""The private folder in your Cloud Storage bucket, plus the small bits of shared
state that both the hourly run and the on-demand voice function read.

    <secret>/state.json        what has been read (written by the hourly run)
    <secret>/settings.json     which senders to always voice (written from the app)
    <secret>/voiced/<id>.json  audio made on demand, waiting for the hourly run to
                               add it to the feed (written by the voice function)
    <secret>/removed/<id>.json emails swiped away in the app, waiting for the
                               hourly run to delete them (written by the voice function)
    <secret>/inbox/<id>.*      text, links and files shared to the app, waiting for
                               the next run (written by the voice function)

Keeping those writers on separate files means the hourly run and the voice
function never overwrite each other's changes.
"""

import json
import os
import re

MONTHLY_CHAR_LIMIT_DEFAULT = 950_000


class Bucket:
    """One folder inside a Google Cloud Storage bucket."""

    def __init__(self, bucket_name: str, folder: str):
        from google.cloud import storage

        self.bucket_name = bucket_name
        self.folder = folder.strip("/")
        self._bucket = storage.Client().bucket(bucket_name)

    def read_json(self, name: str) -> dict | None:
        blob = self._bucket.blob(self._path(name))
        if not blob.exists():
            return None
        return json.loads(blob.download_as_text())

    def read_bytes(self, name: str) -> bytes | None:
        blob = self._bucket.blob(self._path(name))
        return blob.download_as_bytes() if blob.exists() else None

    def write(self, name: str, data: bytes | str, content_type: str, cache: bool = False) -> None:
        blob = self._bucket.blob(self._path(name))
        # Feed and page change often, so tell browsers and podcast apps not to cache them.
        blob.cache_control = "public, max-age=86400" if cache else "no-cache, max-age=0"
        blob.upload_from_string(data, content_type=content_type)

    def delete(self, name: str) -> None:
        blob = self._bucket.blob(self._path(name))
        if blob.exists():
            blob.delete()

    def list(self, prefix: str) -> list[str]:
        """Names (relative to the folder) of files starting with prefix."""
        start = len(self.folder) + 1
        return [b.name[start:] for b in self._bucket.list_blobs(prefix=self._path(prefix))]

    def url(self, name: str) -> str:
        return f"https://storage.googleapis.com/{self.bucket_name}/{self._path(name)}"

    def _path(self, name: str) -> str:
        return f"{self.folder}/{name}"


def monthly_limit() -> int:
    return int((os.environ.get("MONTHLY_CHAR_LIMIT") or "").strip() or MONTHLY_CHAR_LIMIT_DEFAULT)


# Chirp 3 HD voices offered in the app's settings (all in Google's free tier).
# The words are Google's own descriptions of each voice.
VOICES = {
    "en-US-Chirp3-HD-Charon": "Charon · informative",
    "en-US-Chirp3-HD-Orus": "Orus · firm",
    "en-US-Chirp3-HD-Puck": "Puck · upbeat",
    "en-US-Chirp3-HD-Fenrir": "Fenrir · excitable",
    "en-US-Chirp3-HD-Kore": "Kore · firm",
    "en-US-Chirp3-HD-Aoede": "Aoede · breezy",
    "en-US-Chirp3-HD-Leda": "Leda · youthful",
    "en-US-Chirp3-HD-Zephyr": "Zephyr · bright",
}
DEFAULT_VOICE = "en-US-Chirp3-HD-Charon"


def voice_choice(store) -> str:
    """The voice for new audio: the one picked in the app, else TTS_VOICE."""
    saved = (store.read_json("settings.json") or {}).get("voice")
    if saved in VOICES:
        return saved
    return (os.environ.get("TTS_VOICE") or "").strip() or DEFAULT_VOICE


def save_settings(store, now_iso: str, always=None, voice=None) -> dict:
    """Update settings.json, keeping whatever isn't being changed."""
    settings = store.read_json("settings.json") or {}
    if always is not None:
        settings["always"] = clean_addresses(always)
    elif "always" not in settings:
        settings["always"] = always_voice(store)
    if voice is not None:
        if voice not in VOICES:
            raise ValueError("Unknown voice.")
        settings["voice"] = voice
    settings["updated"] = now_iso
    store.write("settings.json", json.dumps(settings), "application/json")
    return settings


def always_voice(store) -> list[str]:
    """Sender addresses whose emails get audio as soon as they arrive. Starts
    from ALWAYS_VOICE until you change it in the app's settings."""
    saved = store.read_json("settings.json")
    if saved is not None and isinstance(saved.get("always"), list):
        return [a for a in saved["always"] if isinstance(a, str)]
    return clean_addresses((os.environ.get("ALWAYS_VOICE") or "").split(","))


def clean_addresses(values) -> list[str]:
    out = []
    for value in values or []:
        value = str(value).strip().lower()
        if re.fullmatch(r"[^@\s]{1,100}@[^@\s]{1,150}", value) and value not in out:
            out.append(value)
    return out[:200]


def voice_used(store, month: str, state: dict | None = None) -> int:
    """Characters voiced this month, including on-demand audio the hourly run
    hasn't counted yet."""
    state = state if state is not None else (store.read_json("state.json") or {})
    used = int(state.get("usage", {}).get(month, 0))
    for name in store.list("voiced/"):
        marker = store.read_json(name) or {}
        if marker.get("month") == month:
            used += int(marker.get("chars", 0))
    return used
