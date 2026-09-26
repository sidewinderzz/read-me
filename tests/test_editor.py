from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import editor
from mail import Email

HTML = """<html><body>
<p>View this email in your browser</p>
<h1>Corn futures jump</h1>
<p>December corn closed at $4.52. <a href="https://example.com/story">Full story</a></p>
<p>Unsubscribe | 123 Main St</p>
</body></html>"""


class FakeClient:
    def __init__(self, reply, stop="end_turn"):
        self.sent = []

        def create(**kw):
            self.sent.append(kw)
            return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=reply)])

        self.messages = SimpleNamespace(create=create)


def _mail(html=HTML, subject="Fwd: ☕️ Corn outlook"):
    return Email("<1@x>", "Farm Journal", subject, datetime(2026, 9, 26, tzinfo=timezone.utc), "", html)


def test_removes_only_the_blocks_claude_names():
    client = FakeClient("REMOVE: 0, 3")
    piece = editor.edit(_mail(), client)
    assert [b.text for b in piece.blocks] == ["Corn futures jump", "December corn closed at $4.52. Full story"]
    assert piece.blocks[1].runs[-1].href == "https://example.com/story"
    assert piece.intro == "From Farm Journal. Corn outlook."
    assert "0 [p] View this email" in client.sent[0]["messages"][0]["content"]


def test_skip_returns_none():
    assert editor.edit(_mail(), FakeClient("SKIP")) is None


def test_parse_reply():
    assert editor.parse_reply("REMOVE: none", 5).remove == set()
    assert editor.parse_reply("REMOVE: 0-2, 4, 9", 5).remove == {0, 1, 2, 4}
    assert editor.parse_reply("Sure.\nREMOVE: 3–1", 5).remove == {1, 2, 3}
    e = editor.parse_reply("REMOVE: 0\nQUIET: 2, 4\nSAY 1-3: Markets were up.\nSAY 4: One more.", 5)
    assert e.quiet == {2, 4} and e.say == [(1, 3, "Markets were up."), (4, 4, "One more.")]
    with pytest.raises(editor.EditError):
        editor.parse_reply("I think blocks 1 and 2", 5)


TICKER = """<html><body>
<h1>Morning news</h1>
<p>Markets</p>
<table><tr><td>Nasdaq</td><td>27,068.72</td><td>+0.48%</td></tr>
<tr><td>Dow</td><td>51,828.62</td><td>+0.93%</td></tr></table>
<p>&mdash;Molly Liebergall, Matty Merritt, Dave Lozo</p>
<p>Stocks swung up yesterday, with the Dow in particular rebounding as investors weighed a long list of news.</p>
</body></html>"""


def test_quiet_and_say_marks_shape_the_audio_not_the_page():
    import article
    client = FakeClient("REMOVE: none\nQUIET: 4\nSAY 1-3: Markets: the Nasdaq rose about half a percent and the Dow almost one percent.")
    piece = editor.edit(_mail(TICKER), client)
    kinds = [b.kind for b in piece.blocks]
    assert kinds == ["h2", "p", "row", "row", "p", "p"]  # everything still on screen
    spoken = [article.spoken_text(b) for b in piece.blocks]
    assert spoken[1].startswith("Markets: the Nasdaq rose")
    assert spoken[2:5] == ["", "", ""]  # rows covered by the sentence; byline quiet
    assert spoken[5].startswith("Stocks swung up yesterday")
    assert piece.blocks[1].group == piece.blocks[3].group == 1


def test_say_cannot_rewrite_real_paragraphs():
    import article
    piece = editor.edit(_mail(TICKER), FakeClient("REMOVE: none\nSAY 5: Stocks went up."))
    assert article.spoken_text(piece.blocks[5]).startswith("Stocks swung up yesterday, with the Dow")
    assert piece.blocks[5].group == -1


def test_removing_everything_is_an_error():
    with pytest.raises(editor.EditError):
        editor.edit(_mail(), FakeClient("REMOVE: 0-10"))


def test_refusal_raises():
    with pytest.raises(editor.EditError):
        editor.edit(_mail(), FakeClient("", stop="refusal"))


def test_workspace_error_becomes_setup_error():
    import anthropic
    import httpx2

    def boom(**kw):
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.BadRequestError("This API key is not scoped to a workspace",
                                        response=httpx2.Response(400, request=req), body=None)

    with pytest.raises(editor.SetupError):
        editor.edit(_mail(), SimpleNamespace(messages=SimpleNamespace(create=boom)))


def test_workspace_header_added_when_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_123")
    assert editor.make_client().default_headers.get("anthropic-workspace-id") == "wrkspc_123"
