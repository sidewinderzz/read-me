from email.message import EmailMessage

from mail import html_to_text, parse_message


def _raw(**kw):
    msg = EmailMessage()
    msg["From"] = kw.get("sender", "Farm Journal <news@example.com>")
    msg["Subject"] = kw.get("subject", "Corn outlook")
    msg["Date"] = "Fri, 25 Sep 2026 06:30:00 -0500"
    if kw.get("message_id", True):
        msg["Message-ID"] = "<abc@example.com>"
    msg.set_content(kw.get("plain", "View this email in your browser"))
    if "html" in kw:
        msg.add_alternative(kw["html"], subtype="html")
    return msg.as_bytes()


def test_prefers_html_and_strips_screen_only_bits():
    html = """<html><head><style>p{color:red}</style></head><body>
      <p>View in browser</p>
      <h1>Corn prices up</h1><p>December futures closed at $4.52, see https://example.com/x</p>
      <img src="x.png"><p>Unsubscribe</p></body></html>"""
    m = parse_message(_raw(html=html))
    assert m.sender == "Farm Journal"
    assert m.subject == "Corn outlook"
    assert m.message_id == "<abc@example.com>"
    assert "Corn prices up" in m.text
    assert "$4.52" in m.text
    assert "https://" not in m.text
    assert "Unsubscribe" not in m.text
    assert "View in browser" not in m.text
    assert "color:red" not in m.text


def test_plain_text_only_email():
    m = parse_message(_raw(plain="Hey, can you check the pivot on the north field?\n\nThanks"))
    assert "north field" in m.text


def test_missing_message_id_gets_stable_fallback():
    a = parse_message(_raw(message_id=False))
    b = parse_message(_raw(message_id=False))
    assert a.message_id and a.message_id == b.message_id


def test_html_paragraphs_survive():
    text = html_to_text("<div>First line</div><div>Second line</div>")
    assert text.splitlines() == ["First line", "Second line"]


def test_reading_address_and_query():
    import mail
    assert mail.reading_address("you@gmail.com") == "you+readme@gmail.com"
    assert mail.reading_address("me+old@gmail.com") == "me+readme@gmail.com"
    q = mail.gmail_query("Read Me/Farm", "me+readme@gmail.com", 7)
    assert q == "{label:read-me-farm to:me+readme@gmail.com} newer_than:7d"


def test_forwarded_email_credits_original_sender():
    body = ("FYI\n\n---------- Forwarded message ---------\n"
            "From: Morning Brew <crew@morningbrew.com>\nDate: Sat, Sep 26, 2026\n"
            "Subject: Cha-Cha Slide\n\nMortgage rates...")
    m = parse_message(_raw(sender="Sam <me@gmail.com>", subject="Fwd: Cha-Cha Slide", plain=body))
    assert m.sender == "Morning Brew"
    assert m.sender_address == "crew@morningbrew.com"
    # Not a forward: keep the real sender even if the body mentions one.
    m = parse_message(_raw(sender="Sam <Me@Gmail.com>", subject="Notes", plain=body))
    assert m.sender == "Sam"
    assert m.sender_address == "me@gmail.com"


def test_fetch_searches_all_mail_and_dedupes(monkeypatch):
    import mail
    calls = []

    class FakeIMAP:
        def __init__(self, host):
            pass

        def login(self, a, p):
            calls.append(("login", a))

        def list(self):
            return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                          b'(\\All \\HasNoChildren) "/" "[Google Mail]/All Mail"']

        def select(self, box, readonly):
            calls.append(("select", box, readonly))
            return "OK", [b"3"]

        def uid(self, cmd, *args):
            calls.append((cmd, args))
            if cmd == "search":
                return "OK", [b"1 2"]
            # uid 1 and 2 are the sent and received copies of the same forward
            return "OK", [(b"x", _raw(subject="Fwd: Hi")), b")"]

        def logout(self):
            pass

    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    got = mail.fetch_to_read("me@gmail.com", "pw", "ReadMe", "me+readme@gmail.com", 7)
    assert len(got) == 1
    assert ("select", '"[Google Mail]/All Mail"', True) in calls
    search = next(c for c in calls if c[0] == "search")
    assert search[1] == (None, "X-GM-RAW", '"{label:readme to:me+readme@gmail.com} newer_than:7d"')
