"""Read Me's on-demand voice: a small Google Cloud function the app calls.

The app sends a POST with JSON: "secret" (your private folder name) and an
"action":

    voice     {"id": "<email id>"}         voice that email now (or return the
                                            audio if it's already been made)
    status    {}                            the always-voice list and how much of
                                            this month's free voice is used
    settings  {"always": [...], "voice": ...} save the always-voice list and/or
                                            the voice for new audio
    sample    {"voice": "<voice name>"}     a short sample of a voice
    remove    {"id": "<email id>"}          remove an email you swiped away (the
                                            hourly run deletes its files)
    add       {"title", "text", "url", "file": {"name", "type", "data" (base64)}}
                                            save something shared from the phone and
                                            start a run to turn it into an article
    check     {}                            look for new mail now: starts the
                                            "Read my emails" workflow on GitHub
    checkstatus {}                          how that run is going

It only ever voices articles already in your folder, so someone who found its
address couldn't use it to read out anything else. Deployed by
.github/workflows/deploy-voice.yml, which copies in article.py, voice.py and
store.py from the repo.
"""

import base64
import binascii
import hmac
import json
import secrets
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import functions_framework

import article
import documents
import store as storage
import voice

ALLOWED_ORIGIN = "https://storage.googleapis.com"  # where the Read Me app lives
SAMPLE_TEXT = ("Here's how I'll sound reading your newsletters. Corn futures closed higher "
               "on Friday, and the weekend looks dry across most of the Midwest.")
CHECK_COOLDOWN = timedelta(seconds=90)
MAX_SHARE_BYTES = 15 * 1024 * 1024
WORKFLOW = "read-emails.yml"

_bucket = None


class Refused(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


@functions_framework.http
def handle(request):
    if request.method == "OPTIONS":
        return "", 204, _cors()
    if request.method != "POST":
        return _reply({"error": "Use POST."}, 405)
    body = request.get_json(silent=True) or {}
    expected = os.environ.get("FEED_SECRET", "")
    if not expected or not hmac.compare_digest(str(body.get("secret", "")).encode(), expected.encode()):
        return _reply({"error": "Not allowed."}, 403)
    try:
        action = body.get("action")
        store = _store()
        speak = lambda said: voice.synthesize(said, voice_name=storage.voice_choice(store))  # noqa: E731
        if action == "voice":
            return _reply(voice_email(store, str(body.get("id", "")), speak=speak))
        if action == "status":
            return _reply(status(store))
        if action == "settings":
            return _reply(save_settings(store, body.get("always"), voice=body.get("voice")))
        if action == "sample":
            return _reply(sample(store, str(body.get("voice", ""))))
        if action == "remove":
            return _reply(remove_email(store, str(body.get("id", ""))))
        if action == "add":
            return _reply(add_shared(store, body))
        if action == "check":
            return _reply(check_now(store))
        if action == "checkstatus":
            return _reply(check_status())
        return _reply({"error": "Unknown action."}, 400)
    except Refused as exc:
        return _reply({"error": str(exc)}, exc.code)


def voice_email(store, email_id: str, speak=voice.synthesize, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if not re.fullmatch(r"[0-9a-f]{12}", email_id):
        raise Refused("That email wasn't found.", 404)
    state = store.read_json("state.json") or {}
    episode = next((e for e in state.get("episodes", []) if e.get("id") == email_id), None)
    if episode is None or not episode.get("text"):
        raise Refused("That email wasn't found. It may have expired.", 404)
    doc = store.read_json(episode["text"])
    if doc is None:
        raise Refused("That email's text is missing. It may have expired.", 404)

    audio_path = f"audio/{email_id}.mp3"
    if episode.get("audio") or "total" in doc:
        return {"url": store.url(episode.get("audio") or audio_path), "doc": doc}

    said, owners = article.segments(doc)
    chars = sum(len(x) for x in said)
    month = now.strftime("%Y-%m")
    used, limit = storage.voice_used(store, month, state), storage.monthly_limit()
    if used + chars > limit:
        raise Refused(f"Voicing this would pass this month's free allowance "
                      f"({used:,} of {limit:,} characters used). You can still read it.", 402)

    audio, seconds = speak(said)
    article.add_timings(doc, owners, seconds)
    store.write(audio_path, audio, "audio/mpeg", cache=True)
    store.write(episode["text"], json.dumps(doc), "application/json")
    # The hourly run picks this up, adds the email to the podcast feed and counts it.
    store.write(f"voiced/{email_id}.json", json.dumps({
        "id": email_id, "audio": audio_path, "bytes": len(audio), "chars": chars,
        "month": month, "at": now.isoformat(),
    }), "application/json")
    return {"url": store.url(audio_path), "doc": doc, "used": used + chars, "limit": limit}


def status(store, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    state = store.read_json("state.json") or {}
    senders = [{"address": a, "name": v.get("name", a)} for a, v in state.get("senders", {}).items()]
    return {
        "always": storage.always_voice(store),
        "senders": senders,
        "used": storage.voice_used(store, now.strftime("%Y-%m"), state),
        "limit": storage.monthly_limit(),
        "voice": storage.voice_choice(store),
        "voices": [{"name": k, "label": v} for k, v in storage.VOICES.items()],
    }


def save_settings(store, always=None, voice=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if always is None and voice is None:
        raise Refused("Settings weren't understood.", 400)
    if always is not None and not isinstance(always, list):
        raise Refused("Settings weren't understood.", 400)
    try:
        storage.save_settings(store, now.isoformat(), always=always, voice=voice)
    except ValueError as exc:
        raise Refused(str(exc), 400) from exc
    return status(store, now)


def sample(store, voice_name: str, speak=None) -> dict:
    """A short clip of a voice, made once and kept."""
    if voice_name not in storage.VOICES:
        raise Refused("Unknown voice.", 400)
    path = f"samples/{voice_name}.mp3"
    if store.read_json(path.replace(".mp3", ".json")) is None:
        speak = speak or (lambda said: voice.synthesize(said, voice_name=voice_name))
        audio, _ = speak([SAMPLE_TEXT])
        store.write(path, audio, "audio/mpeg", cache=True)
        store.write(path.replace(".mp3", ".json"), json.dumps({"voice": voice_name}), "application/json")
    return {"url": store.url(path)}


def add_shared(store, body: dict, now: datetime | None = None, github=None) -> dict:
    """Save text, a link or a file shared from the phone, then start a run."""
    now = now or datetime.now(timezone.utc)
    title = str(body.get("title") or "").strip()[:300]
    text = str(body.get("text") or "").strip()[:200_000]
    url = str(body.get("url") or "").strip()[:2000]
    item_id = secrets.token_hex(6)
    meta = {"id": item_id, "title": title, "text": text, "url": url, "at": now.isoformat()}
    shared_file = body.get("file")
    if shared_file:
        if not isinstance(shared_file, dict):
            raise Refused("That file wasn't understood.", 400)
        name = str(shared_file.get("name") or "file")[:200]
        kind = documents.kind_of(name, str(shared_file.get("type") or ""))
        if not kind:
            raise Refused("Read Me can read PDFs, Word documents, text files and web pages.", 415)
        try:
            data = base64.b64decode(str(shared_file.get("data") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise Refused("That file didn't come through. Try sharing it again.", 400) from exc
        if not data or len(data) > MAX_SHARE_BYTES:
            raise Refused("That file is empty or too big (over 15 MB).", 413)
        store.write(f"inbox/{item_id}.bin", data, "application/octet-stream")
        meta.update(filename=name, content_type=kind)
    elif not (text or url):
        raise Refused("There was nothing to add.", 400)
    store.write(f"inbox/{item_id}.json", json.dumps(meta), "application/json")
    try:
        started = check_now(store, now, github=github, force=True)["started"]
    except Refused:
        started = False  # "check now" isn't set up; the next scheduled run picks it up
    return {"added": item_id, "checking": started, "at": now.isoformat()}


def check_now(store, now: datetime | None = None, github=None, force: bool = False) -> dict:
    """Start the hourly "Read my emails" run right away."""
    now = now or datetime.now(timezone.utc)
    github = github or _github
    last = (store.read_json("checked.json") or {}).get("at")
    if not force and last and now - datetime.fromisoformat(last) < CHECK_COOLDOWN:
        return {"started": False, "at": last}  # one is already on its way
    ref = os.environ.get("GH_REF") or "main"
    github("POST", f"actions/workflows/{WORKFLOW}/dispatches", {"ref": ref})
    store.write("checked.json", json.dumps({"at": now.isoformat()}), "application/json")
    return {"started": True, "at": now.isoformat()}


def check_status(github=None) -> dict:
    github = github or _github
    runs = github("GET", f"actions/workflows/{WORKFLOW}/runs?per_page=1") or {}
    run = (runs.get("workflow_runs") or [{}])[0]
    return {"status": run.get("status"), "conclusion": run.get("conclusion"), "created": run.get("created_at")}


def _github(method: str, path: str, body: dict | None = None):
    token, repo = os.environ.get("GH_TOKEN"), os.environ.get("GH_REPO")
    if not token or not repo:
        raise Refused("Checking for new mail isn't set up yet (see the README).", 501)
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "read-me-voice"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise Refused(f"GitHub said no ({exc.code}). The access key may have expired.", 502) from exc
    except urllib.error.URLError as exc:
        raise Refused("Couldn't reach GitHub. Try again in a minute.", 502) from exc
    return json.loads(raw) if raw else None


def remove_email(store, email_id: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if not re.fullmatch(r"[0-9a-f]{12}", email_id):
        raise Refused("That email wasn't found.", 404)
    # The hourly run owns state.json, so leave a note for it rather than editing it here.
    store.write(f"removed/{email_id}.json", json.dumps({"id": email_id, "at": now.isoformat()}),
                "application/json")
    return {"removed": email_id}


def _store():
    global _bucket
    if _bucket is None:
        _bucket = storage.Bucket(os.environ["GCS_BUCKET"], os.environ["FEED_SECRET"])
    return _bucket


def _cors() -> dict:
    return {
        "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "3600",
        "Vary": "Origin",
    }


def _reply(data: dict, code: int = 200):
    return json.dumps(data), code, {**_cors(), "Content-Type": "application/json"}
