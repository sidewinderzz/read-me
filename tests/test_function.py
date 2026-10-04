import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import FakeStore

spec = importlib.util.spec_from_file_location("voice_function", Path(__file__).parent.parent / "function" / "main.py")
fn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fn)

NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)
ID = "abcdef123456"


def _store():
    store = FakeStore()
    store.files["state.json"] = json.dumps({
        "episodes": [{"id": ID, "title": "T", "sender": "S", "published": NOW.isoformat(), "audio": "",
                      "bytes": 0, "chars": 40, "text": f"text/{ID}.json", "sender_address": "s@x.com"}],
        "usage": {"2026-09": 100}, "senders": {"s@x.com": {"name": "S", "last": NOW.isoformat()}},
    })
    store.files[f"text/{ID}.json"] = json.dumps({"version": 3, "intro": "From S. T.", "blocks": [
        {"kind": "p", "runs": [{"text": "First."}]},
        {"kind": "img", "src": "https://x/i.png", "alt": "", "href": ""},
        {"kind": "p", "runs": [{"text": "Second."}]},
    ]})
    return store


def _speak(segments):
    return b"A" * len(segments), [1.5] * len(segments)


def test_voice_email_makes_audio_timings_and_marker():
    store = _store()
    out = fn.voice_email(store, ID, speak=_speak, now=NOW)
    assert out["url"].endswith(f"audio/{ID}.mp3")
    assert [b.get("t") for b in out["doc"]["blocks"]] == [1.5, None, 3.0]
    assert store.files[f"audio/{ID}.mp3"] == b"AAA"
    assert json.loads(store.files[f"text/{ID}.json"])["total"] == 4.5
    marker = json.loads(store.files[f"voiced/{ID}.json"])
    assert marker == {"id": ID, "audio": f"audio/{ID}.mp3", "bytes": 3, "chars": len("From S. T.First.Second."),
                      "month": "2026-09", "at": NOW.isoformat()}
    assert out["used"] == 100 + marker["chars"]


def test_voicing_twice_reuses_the_audio():
    store = _store()
    fn.voice_email(store, ID, speak=_speak, now=NOW)
    calls = []
    out = fn.voice_email(store, ID, speak=lambda s: calls.append(s) or _speak(s), now=NOW)
    assert calls == [] and out["url"].endswith(f"audio/{ID}.mp3")


def test_voice_refuses_unknown_ids_and_over_allowance(monkeypatch):
    store = _store()
    with pytest.raises(fn.Refused) as err:
        fn.voice_email(store, "../../etc", speak=_speak, now=NOW)
    assert err.value.code == 404
    monkeypatch.setenv("MONTHLY_CHAR_LIMIT", "110")
    with pytest.raises(fn.Refused) as err:
        fn.voice_email(store, ID, speak=_speak, now=NOW)
    assert err.value.code == 402 and "allowance" in str(err.value)


def test_settings_are_cleaned_and_saved():
    store = _store()
    out = fn.save_settings(store, ["S@X.com", "not an address", "s@x.com", "b@y.org"], now=NOW)
    assert json.loads(store.files["settings.json"])["always"] == ["s@x.com", "b@y.org"]
    assert out["always"] == ["s@x.com", "b@y.org"]
    assert out["senders"] == [{"address": "s@x.com", "name": "S"}]
    assert out["used"] == 100
    with pytest.raises(fn.Refused):
        fn.save_settings(store, "s@x.com")


def test_handle_checks_the_secret_and_answers_cors(monkeypatch):
    from flask import Request
    from werkzeug.test import EnvironBuilder

    monkeypatch.setenv("FEED_SECRET", "right")
    store = _store()
    monkeypatch.setattr(fn, "_store", lambda: store)

    def call(method="POST", body=None):
        env = EnvironBuilder(method=method, json=body).get_environ()
        return fn.handle(Request(env))

    body, code, headers = call("OPTIONS")
    assert code == 204 and headers["Access-Control-Allow-Origin"] == "https://storage.googleapis.com"
    assert call(body={"secret": "wrong", "action": "status"})[1] == 403
    body, code, _ = call(body={"secret": "right", "action": "status"})
    assert code == 200 and json.loads(body)["senders"][0]["address"] == "s@x.com"
    assert call(body={"secret": "right", "action": "nope"})[1] == 400
    assert call(body={"secret": "right", "action": "voice", "id": "zzz"})[1] == 404


def test_remove_leaves_a_note_for_the_hourly_run():
    store = _store()
    assert fn.remove_email(store, ID, now=NOW) == {"removed": ID}
    assert json.loads(store.files[f"removed/{ID}.json"]) == {"id": ID, "at": NOW.isoformat()}
    with pytest.raises(fn.Refused):
        fn.remove_email(store, "../state")


def test_voice_choice_is_saved_and_used(monkeypatch):
    import store as storage
    store = _store()
    monkeypatch.delenv("TTS_VOICE", raising=False)
    assert storage.voice_choice(store) == storage.DEFAULT_VOICE
    out = fn.save_settings(store, voice="en-US-Chirp3-HD-Kore", now=NOW)
    assert out["voice"] == "en-US-Chirp3-HD-Kore" and len(out["voices"]) == len(storage.VOICES)
    fn.save_settings(store, ["a@b.com"], now=NOW)  # changing one setting keeps the other
    saved = json.loads(store.files["settings.json"])
    assert saved["voice"] == "en-US-Chirp3-HD-Kore" and saved["always"] == ["a@b.com"]
    with pytest.raises(fn.Refused):
        fn.save_settings(store, voice="en-US-Some-Paid-Voice", now=NOW)


def test_samples_are_made_once():
    store = _store()
    calls = []
    speak = lambda said: calls.append(said) or (b"S", [1.0])  # noqa: E731
    out = fn.sample(store, "en-US-Chirp3-HD-Puck", speak=speak)
    fn.sample(store, "en-US-Chirp3-HD-Puck", speak=speak)
    assert out["url"].endswith("samples/en-US-Chirp3-HD-Puck.mp3") and len(calls) == 1
    with pytest.raises(fn.Refused):
        fn.sample(store, "../../x", speak=speak)


def test_check_now_starts_the_workflow_once_per_cooldown(monkeypatch):
    from datetime import timedelta
    monkeypatch.setenv("GH_REF", "claude/voice-api-email-reader-1u3569")
    store = _store()
    calls = []
    github = lambda method, path, body=None: calls.append((method, path, body))  # noqa: E731
    assert fn.check_now(store, now=NOW, github=github)["started"] is True
    assert calls == [("POST", "actions/workflows/read-emails.yml/dispatches",
                      {"ref": "claude/voice-api-email-reader-1u3569"})]
    assert fn.check_now(store, now=NOW + timedelta(seconds=30), github=github)["started"] is False
    assert fn.check_now(store, now=NOW + timedelta(minutes=2), github=github)["started"] is True
    status = fn.check_status(github=lambda m, p, b=None: {"workflow_runs": [
        {"status": "completed", "conclusion": "success", "created_at": "2026-09-26T22:00:00Z"}]})
    assert status == {"status": "completed", "conclusion": "success", "created": "2026-09-26T22:00:00Z"}


def test_check_now_says_when_it_is_not_set_up(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(fn.Refused) as err:
        fn.check_now(_store(), now=NOW)
    assert err.value.code == 501


def _github_calls():
    calls = []

    def github(method, path, body=None):
        calls.append((method, path, body))
        return {}

    return calls, github


def test_add_shared_link_saves_it_and_starts_a_run_even_right_after_one():
    store = FakeStore()
    store.files["checked.json"] = json.dumps({"at": NOW.isoformat()})  # a check just happened
    calls, github = _github_calls()
    out = fn.add_shared(store, {"title": "Storm", "url": "https://news.example.com/rain"}, now=NOW, github=github)
    meta = json.loads(store.files[f"inbox/{out['added']}.json"])
    assert meta["url"] == "https://news.example.com/rain" and meta["title"] == "Storm"
    assert out["checking"] is True and calls[0][0] == "POST"


def test_add_shared_file():
    import base64

    store = FakeStore()
    calls, github = _github_calls()
    data = base64.b64encode(b"%PDF-1.4 hi").decode()
    out = fn.add_shared(store, {"file": {"name": "notice.pdf", "type": "application/pdf", "data": data}},
                        now=NOW, github=github)
    assert store.files[f"inbox/{out['added']}.bin"] == b"%PDF-1.4 hi"
    meta = json.loads(store.files[f"inbox/{out['added']}.json"])
    assert meta["filename"] == "notice.pdf" and meta["content_type"] == "application/pdf"


@pytest.mark.parametrize("body, code", [
    ({}, 400),
    ({"file": "x"}, 400),
    ({"file": {"name": "photo.jpg", "type": "image/jpeg", "data": "aGk="}}, 415),
    ({"file": {"name": "a.pdf", "data": "not base64!"}}, 400),
    ({"file": {"name": "a.pdf", "data": ""}}, 413),
])
def test_add_shared_refuses_bad_input(body, code):
    store = FakeStore()
    with pytest.raises(fn.Refused) as err:
        fn.add_shared(store, body, now=NOW, github=_github_calls()[1])
    assert err.value.code == code and not store.files


def test_add_shared_without_check_now_set_up_still_saves(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    store = FakeStore()
    out = fn.add_shared(store, {"text": "some words to read"}, now=NOW)
    assert out["checking"] is False and f"inbox/{out['added']}.json" in store.files
