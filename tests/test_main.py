import json
from datetime import datetime, timedelta, timezone

import pytest

import editor
import main
from article import Block, Run
from conftest import FakeStore
from mail import Email

NOW = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
ALWAYS = "news@always.com"


def _mail(n, subject=None, sender=ALWAYS):
    return Email(message_id=f"<m{n}@x>", sender="Sender", subject=subject or f"Fwd: Letter {n}",
                 date=NOW, text=f"Body {n}", sender_address=sender)


def _article(m, words="Paragraph one."):
    return editor.Article(intro=f"From Sender. {m.subject}.", blocks=[
        Block("h2", [Run("Headline")]),
        Block("img", src="https://img.example.com/a.jpg", alt="A photo"),
        Block("p", [Run(words + " "), Run("link", href="https://example.com")]),
    ])


def _speak(segments):
    return b"MP3" * len(segments), [2.0] * len(segments)


def _store(always=(ALWAYS,)):
    store = FakeStore()
    store.files["settings.json"] = json.dumps({"always": list(always)})
    return store


def _run(store, emails, edit=_article, speak=_speak, **kw):
    opts = dict(now=NOW, max_emails=10, monthly_char_limit=950_000, keep_days=30, title="Read Me")
    opts.update(kw)
    return main.run(lambda: emails, edit, speak, store, **opts)


def _state(store):
    return json.loads(store.files["state.json"])


def test_always_voice_sender_gets_audio_and_timings():
    store = _store()
    summary = _run(store, [_mail(1)])
    assert (summary["read"], summary["voiced"]) == (1, 1)
    ep = _state(store)["episodes"][0]
    assert ep["title"] == "Letter 1" and ep["audio"] == f"audio/{ep['id']}.mp3" and ep["bytes"] > 0
    doc = json.loads(store.files[ep["text"]])
    assert [b["kind"] for b in doc["blocks"]] == ["h2", "img", "p"]
    assert doc["blocks"][0]["t"] == 2.0 and "t" not in doc["blocks"][1] and doc["blocks"][2]["t"] == 4.0
    assert doc["total"] == 6.0 and doc["intro"] == "From Sender. Fwd: Letter 1."
    assert doc["blocks"][2]["runs"][1] == {"text": "link", "href": "https://example.com"}
    assert _state(store)["usage"]["2026-09"] == ep["chars"]
    assert "<item>" in store.files["feed.xml"]


def test_other_senders_are_ready_to_read_but_not_voiced():
    store = _store()
    spoken = []
    summary = _run(store, [_mail(1, sender="other@x.com")], speak=lambda s: spoken.append(s) or _speak(s))
    assert (summary["read"], summary["voiced"]) == (1, 0)
    assert spoken == []
    ep = _state(store)["episodes"][0]
    assert ep["audio"] == "" and ep["bytes"] == 0 and ep["chars"] > 0
    assert "total" not in json.loads(store.files[ep["text"]])
    assert "<item>" not in store.files["feed.xml"]  # nothing to play yet
    assert _state(store)["usage"] == {}
    assert _state(store)["senders"]["other@x.com"]["name"] == "Sender"


def test_default_always_list_comes_from_env(monkeypatch):
    monkeypatch.setenv("ALWAYS_VOICE", "A@X.com, b@y.com")
    store = FakeStore()
    assert _run(store, [_mail(1, sender="a@x.com")])["voiced"] == 1


def test_audio_made_in_the_app_joins_the_feed_on_the_next_run():
    store = _store(always=())
    _run(store, [_mail(1)])
    ep = _state(store)["episodes"][0]
    store.files[f"audio/{ep['id']}.mp3"] = b"x"
    store.files[f"voiced/{ep['id']}.json"] = json.dumps(
        {"id": ep["id"], "audio": f"audio/{ep['id']}.mp3", "bytes": 1234, "chars": 500, "month": "2026-09"})
    _run(store, [])
    state = _state(store)
    assert state["episodes"][0]["audio"] == f"audio/{ep['id']}.mp3"
    assert state["episodes"][0]["bytes"] == 1234
    assert state["usage"]["2026-09"] == 500
    assert not any(n.startswith("voiced/") for n in store.files)
    assert "<item>" in store.files["feed.xml"]


def test_second_run_skips_already_read():
    store = _store()
    _run(store, [_mail(1)])
    calls = []
    summary = _run(store, [_mail(1), _mail(2)], edit=lambda m: calls.append(m.subject) or _article(m))
    assert calls == ["Fwd: Letter 2"]
    assert summary["read"] == 1


def test_failures_retry_then_give_up():
    store = _store()

    def boom(segments):
        raise RuntimeError("voice down")

    for _ in range(main.GIVE_UP_AFTER):
        assert _run(store, [_mail(1)], speak=boom)["failed"] == 1
    assert _run(store, [_mail(1)], speak=boom)["failed"] == 0  # given up
    assert _state(store)["processed"]["<m1@x>"]["attempts"] == main.GIVE_UP_AFTER


def test_one_failure_does_not_stop_the_rest():
    store = _store()

    def speak(segments):
        if "Letter 1" in segments[0]:
            raise RuntimeError("bad")
        return _speak(segments)

    summary = _run(store, [_mail(1), _mail(2)], speak=speak)
    assert (summary["read"], summary["failed"]) == (1, 1)


def test_over_the_allowance_it_is_still_ready_to_read():
    store = _store()
    long = lambda m: _article(m, words="x" * 400)  # noqa: E731
    summary = _run(store, [_mail(1), _mail(2), _mail(3)], edit=long, monthly_char_limit=1000)
    assert (summary["read"], summary["voiced"], summary["over_budget"]) == (3, 2, 1)
    eps = _state(store)["episodes"]
    assert [bool(e["audio"]) for e in eps] == [True, True, False]


def test_quiet_run_writes_nothing():
    store = _store()
    _run(store, [])
    writes = []
    store.write = lambda *a, **k: writes.append(a[0])
    _run(store, [])
    assert writes == []


def test_old_episodes_are_deleted():
    store = _store()
    _run(store, [_mail(1), _mail(2, sender="z@z.com")], now=NOW - timedelta(days=40))
    assert any(f.startswith("audio/") for f in store.files)
    _run(store, [], now=NOW)
    assert not any(f.startswith(("audio/", "text/")) for f in store.files)
    assert _state(store)["episodes"] == []


def test_codes_and_confirmations_are_skipped_once():
    store = _store()
    calls = []

    def edit(m):
        calls.append(m.subject)
        return None if "code" in m.subject else _article(m)

    emails = [_mail(1, subject="Your sign-in code"), _mail(2)]
    summary = _run(store, emails, edit=edit)
    assert summary["read"] == 1 and summary["not_worth_reading"] == 1
    _run(store, emails, edit=edit)
    assert calls.count("Your sign-in code") == 1


def test_setup_error_stops_run_without_using_up_retries():
    store = _store()
    calls = []

    def edit(m):
        calls.append(m)
        raise editor.SetupError("key not scoped to a workspace")

    summary = _run(store, [_mail(1), _mail(2)], edit=edit)
    assert len(calls) == 1
    assert "workspace" in summary["setup_error"]
    assert _state(store)["processed"] == {}
    assert "index.html" in store.files


@pytest.mark.parametrize("raw,clean", [("Fwd: Hi", "Hi"), ("FW: Fwd: Hi", "Hi"), ("Re: Hi", "Re: Hi"), ("Fwd:", "(no subject)")])
def test_clean_subject(raw, clean):
    from mail import clean_subject
    assert clean_subject(raw) == clean


def test_a_changed_app_page_is_published_even_without_new_mail(monkeypatch):
    import podcast
    store = _store()
    _run(store, [])
    writes = []
    real_write = store.write
    store.write = lambda name, *a, **k: writes.append(name) or real_write(name, *a, **k)
    monkeypatch.setattr(podcast, "PLAYER_TEMPLATE", podcast.PLAYER_TEMPLATE + "<!-- new version -->")
    _run(store, [])
    assert "index.html" in writes
    writes.clear()
    _run(store, [])
    assert writes == []  # and only once


def test_swiped_away_emails_are_deleted_on_the_next_run():
    store = _store()
    _run(store, [_mail(1), _mail(2)])
    first, second = _state(store)["episodes"]
    store.files[f"removed/{first['id']}.json"] = json.dumps({"id": first["id"]})
    _run(store, [_mail(1), _mail(2)])  # Gmail still has them; they must not come back
    state = _state(store)
    assert [e["id"] for e in state["episodes"]] == [second["id"]]
    assert first["audio"] not in store.files and first["text"] not in store.files
    assert not any(n.startswith("removed/") for n in store.files)
    assert "Letter 1" not in store.files["feed.xml"] and "Letter 2" in store.files["feed.xml"]
