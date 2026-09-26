"""Pull the emails you've marked for reading out of Gmail.

Two ways an email gets read:
  - it carries the Gmail label (default "ReadMe"), or
  - it was sent to your reading address (default you+readme@gmail.com).

The second one matters because Gmail doesn't run filters on mail you forward
to yourself, so a forwarded email would otherwise never get the label.
Signing in uses a Gmail app password, not your normal password.
"""

import email
import email.utils
import hashlib
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy

from bs4 import BeautifulSoup

IMAP_HOST = "imap.gmail.com"

# Lines like these only make sense on a screen; dropping them before Claude sees
# the email saves tokens. Claude cleans up whatever slips through.
_SCREEN_ONLY = re.compile(
    r"^(view (this email )?in (your )?browser|unsubscribe|manage (your )?preferences"
    r"|update (your )?preferences|click here|read online|forward to a friend)\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://\S+|www\.\S+")


@dataclass
class Email:
    message_id: str
    sender: str
    subject: str
    date: datetime
    text: str  # readable plain text
    html: str = ""  # the original HTML part, for the reading view
    sender_address: str = ""  # lower-case email address, for the "always voice" list


def reading_address(address: str) -> str:
    """you@gmail.com -> you+readme@gmail.com"""
    local, _, domain = address.partition("@")
    return f"{local.split('+')[0]}+readme@{domain}"


def gmail_query(label: str, forward_to: str, since_days: int) -> str:
    # Gmail search spells labels in lowercase with spaces and slashes as dashes.
    slug = re.sub(r"[\s/]+", "-", label.strip()).lower()
    return f"{{label:{slug} to:{forward_to}}} newer_than:{since_days}d"


def fetch_to_read(address: str, app_password: str, label: str, forward_to: str,
                  since_days: int) -> list[Email]:
    """Every recent email that's labeled for reading or was sent to the
    reading address, oldest first."""
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        conn.login(address, app_password)
        status, _ = conn.select(_all_mail(conn), readonly=True)
        if status != "OK":
            raise RuntimeError("Couldn't open All Mail in Gmail. Is IMAP access turned on?")
        query = gmail_query(label, forward_to, since_days)
        _, data = conn.uid("search", None, "X-GM-RAW", _quote(query))
        emails, seen = [], set()
        for uid in data[0].split():
            _, parts = conn.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if not raw:
                continue
            try:
                m = parse_message(raw)
            except Exception as exc:  # a malformed email shouldn't block the others
                print(f"  Skipping an email Gmail returned that couldn't be parsed: {exc}")
                continue
            if m.message_id not in seen:
                seen.add(m.message_id)
                emails.append(m)
        return sorted(emails, key=lambda e: e.date)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _all_mail(conn) -> str:
    """Gmail's All Mail folder name depends on the account's language, so find
    it by its \\All flag rather than by name."""
    _, folders = conn.list()
    for line in folders or []:
        line = line.decode() if isinstance(line, bytes) else str(line)
        match = re.search(r"\\All\b.*?\) \"[^\"]*\" (.+)$", line)
        if match:
            name = match.group(1).strip()
            return name if name.startswith('"') else _quote(name)
    return _quote("[Gmail]/All Mail")


def parse_message(raw: bytes) -> Email:
    msg = email.message_from_bytes(raw, policy=policy.default)
    subject = str(msg.get("Subject", "") or "").strip() or "(no subject)"
    name, addr = email.utils.parseaddr(str(msg.get("From", "")))
    sender = name or addr or "Unknown sender"
    address = addr.lower()
    try:
        date = email.utils.parsedate_to_datetime(str(msg.get("Date")))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        date = datetime.now(timezone.utc)

    message_id = str(msg.get("Message-ID", "") or "").strip()
    if not message_id:
        # Rare, but without one we still need a stable key to avoid re-reading it.
        message_id = hashlib.sha1(f"{sender}|{subject}|{date.isoformat()}".encode()).hexdigest()

    text = body_text(msg)
    if re.match(r"\s*fwd?\s*:", subject, re.I):
        # A forward: credit the original sender, not you.
        fwd_name, fwd_addr = _forwarded_sender(text)
        sender, address = fwd_name or fwd_addr or sender, fwd_addr or address
    html_part = msg.get_body(preferencelist=("html",))
    html = html_part.get_content() if html_part is not None else ""
    return Email(message_id=message_id, sender=sender, subject=subject, date=date,
                 text=text, html=html, sender_address=address)


def clean_subject(subject: str) -> str:
    """ "Fwd: FW: Hello" -> "Hello" """
    return re.sub(r"^(\s*fwd?\s*:\s*)+", "", subject, flags=re.IGNORECASE).strip() or "(no subject)"


def _forwarded_sender(text: str) -> tuple[str, str]:
    """(name, address) of the original sender in a forwarded message's header block."""
    match = re.search(r"forwarded message.*?\n\s*From:\s*([^\n]+)", text, re.IGNORECASE | re.DOTALL)
    if not match:
        return "", ""
    name, addr = email.utils.parseaddr(match.group(1).strip())
    return name, addr.lower()


def body_text(msg) -> str:
    """The readable text of an email, preferring the HTML part (newsletters'
    plain-text parts are often just "view this in your browser")."""
    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        text = html_to_text(html.get_content())
        if text:
            return text
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        return clean_text(plain.get_content())
    return ""


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "title", "meta", "img", "svg", "noscript"]):
        tag.decompose()
    # Put block elements on their own lines so paragraphs survive.
    for tag in soup.find_all(["p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"]):
        tag.insert_before("\n")
    return clean_text(soup.get_text())


def clean_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = _URL.sub("", line.replace("‌", "").replace("\xa0", " "))
        line = re.sub(r"[ \t]+", " ", line).strip()
        if _SCREEN_ONLY.match(line):
            continue
        lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _quote(label: str) -> str:
    return '"' + label.replace("\\", "\\\\").replace('"', '\\"') + '"'
