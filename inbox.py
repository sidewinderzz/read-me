"""Things shared to Read Me from the phone's share menu (text, links, files).

The voice function saves each one in the bucket as inbox/<id>.json (plus
inbox/<id>.bin for a file) and starts a run; the run turns each item into an
email-like message so it goes through the same editor, reader and voice.
"""

from datetime import datetime, timezone

from documents import Attachment
from mail import Email

PREFIX = "shared:"


def items(store) -> list[Email]:
    found = []
    for name in store.list("inbox/"):
        if not name.endswith(".json"):
            continue
        meta = store.read_json(name) or {}
        item_id = meta.get("id")
        if not item_id:
            continue
        attachments = []
        if meta.get("filename"):
            data = store.read_bytes(f"inbox/{item_id}.bin")
            if data:
                attachments.append(Attachment(meta["filename"], meta.get("content_type", ""), data))
        text = "\n\n".join(x for x in (meta.get("text"), meta.get("url")) if x)
        try:
            when = datetime.fromisoformat(meta.get("at", ""))
        except ValueError:
            when = datetime.now(timezone.utc)
        found.append(Email(
            message_id=PREFIX + item_id,
            sender="Shared",
            subject=meta.get("title") or meta.get("filename") or _first_words(text) or "Shared",
            date=when,
            text=text,
            attachments=attachments,
        ))
    return found


def remove(store, message_id: str) -> None:
    item_id = message_id[len(PREFIX):]
    store.delete(f"inbox/{item_id}.bin")
    store.delete(f"inbox/{item_id}.json")


def _first_words(text: str, n: int = 10) -> str:
    words = text.split()
    if not words or words[0].startswith("http"):
        return ""
    return " ".join(words[:n]) + ("…" if len(words) > n else "")
