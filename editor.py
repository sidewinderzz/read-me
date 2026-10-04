"""Have Claude act as the editor: it reads a numbered outline of the email and
says which blocks are clutter (footers, ads, "view in browser"...), which to
show but not read aloud (bylines, photo credits), and gives a one-sentence
spoken version of data tables like a market ticker. It never rewrites the
stories, so what you hear and read is the author's own words.

The instructions Claude follows live in prompt.md.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

import article
import documents
import inbox
from mail import Email, clean_subject

# Haiku is the cheapest Claude model and plenty for sorting blocks: well
# under a cent per email. Set CLAUDE_MODEL to try a bigger model.
DEFAULT_MODEL = "claude-haiku-4-5"

PROMPT = (Path(__file__).parent / "prompt.md").read_text()


class EditError(Exception):
    pass


class SetupError(Exception):
    """A problem with the Claude account or key rather than with one email,
    so there's no point trying the rest of the emails this run."""


# SAY may only stand in for short, data-like blocks, never real paragraphs.
SAY_MAX_BLOCK_CHARS = 120  # a label or note inside a group that has table rows
SAY_MAX_LINE_CHARS = 40  # otherwise every line must be this short (e.g. "Mon: 72°, sunny")
SAY_MAX_BLOCKS = 40


@dataclass
class Edits:
    remove: set[int] = field(default_factory=set)
    quiet: set[int] = field(default_factory=set)
    say: list[tuple[int, int, str]] = field(default_factory=list)


@dataclass
class Article:
    intro: str  # spoken before the first block, e.g. "From Morning Brew. Cha-Cha Slide."
    blocks: list[article.Block]
    title: str = ""  # set when it isn't the email's subject (a linked page, a shared file)
    source: str = ""  # set when it isn't the sender (the website a link came from)


# Phone and mail-app sign-offs that don't count as a real message.
_SIGNOFF = re.compile(r"^(sent from my \w+|get outlook for \w+|sent via \w+|shared (from|via) \w+)", re.I)


def gather(mail: Email, fetch=documents.fetch_page) -> tuple[list[article.Block], str, str]:
    """All the blocks for one email: its own text, the page it links to (if
    it's basically just a link), and any readable attachments. Also returns a
    title and source when those should replace the subject and sender."""
    body = article.extract(mail.html, mail.text)
    title = source = ""

    def says_something(block):
        words = documents._URL.sub(" ", article.spoken_text(block) or "").split()
        return len(words) >= 3 and not _SIGNOFF.match(" ".join(words))

    # Keep the email's own text whole if it says anything; drop it if it's
    # only a sign-off or the bare link.
    note = body if any(says_something(b) for b in body) else []
    extra: list[article.Block] = []

    link = None if mail.attachments else documents.lone_link(mail.text, mail.subject)
    if link:
        try:
            page = fetch(link)
            extra += page.blocks
            title, source = page.title, page.site
        except documents.DocumentError as exc:
            extra.append(article.Block("p", [article.Run(f"This link couldn't be read: {exc}")]))

    for att in mail.attachments:
        name = att.filename or "Attachment"
        try:
            doc = documents.file_blocks(att)
        except documents.DocumentError as exc:
            doc = [article.Block("p", [article.Run(str(exc))])]
        if note or extra or len(mail.attachments) > 1:
            extra.append(article.Block("h2", [article.Run(f"📎 {name}")]))
        elif not title:
            title = documents.title_from_filename(name)
        extra += doc

    if not extra:
        return body, "", ""
    return (note + extra)[:documents.MAX_BLOCKS], title, source





def make_client() -> anthropic.Anthropic:
    # Organization-level API keys must name a workspace on every request.
    workspace = (os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
    headers = {"anthropic-workspace-id": workspace} if workspace else None
    return anthropic.Anthropic(default_headers=headers)


def edit(mail: Email, client: anthropic.Anthropic | None = None) -> Article | None:
    """The email with its clutter removed, or None when it isn't worth reading
    at all (verification codes, confirm-your-email requests and the like)."""
    blocks, title, source = gather(mail)
    if not blocks:
        raise EditError("This email has no readable content.")

    outline = article.outline(blocks)
    if len(outline) > 100_000:
        outline = article.outline(blocks, limit=80)
    content = (
        f"From: {mail.sender}\n"
        f"Subject: {mail.subject}\n\n"
        f"<blocks>\n{outline}\n</blocks>"
    )

    reply = _ask(client or make_client(), content)
    edits = parse_reply(reply, len(blocks))
    if edits is None:
        return None
    kept = apply(blocks, edits)
    if not any(article.spoken_text(b) for b in kept):
        raise EditError("Nothing was left to read after removing the clutter.")
    return Article(intro=spoken_intro(mail, title, source), blocks=kept, title=title, source=source)


def parse_reply(reply: str, count: int) -> Edits | None:
    """Claude's marks, or None for SKIP."""
    reply = reply.strip()
    if re.match(r"skip\b", reply, re.IGNORECASE):
        return None
    remove = re.search(r"^\s*REMOVE\s*:\s*([^\n]*)", reply, re.IGNORECASE | re.MULTILINE)
    says = re.findall(r"^\s*SAY\s+(\d+)(?:\s*[-–]\s*(\d+))?\s*:\s*(.+)$", reply, re.IGNORECASE | re.MULTILINE)
    quiet = re.search(r"^\s*QUIET\s*:\s*([^\n]*)", reply, re.IGNORECASE | re.MULTILINE)
    if not (remove or says or quiet):
        raise EditError(f"Claude's reply wasn't understood: {reply[:120]!r}")
    return Edits(
        remove=_numbers(remove.group(1), count) if remove else set(),
        quiet=_numbers(quiet.group(1), count) if quiet else set(),
        say=[(int(lo), int(hi or lo), text.strip()) for lo, hi, text in says],
    )


def apply(blocks: list[article.Block], edits: Edits) -> list[article.Block]:
    """The blocks that are kept, with quiet and spoken-sentence marks set."""
    position, kept = {}, []
    for i, block in enumerate(blocks):
        if i not in edits.remove:
            position[i] = len(kept)
            kept.append(block)
    for i in edits.quiet:
        if i in position:
            kept[position[i]].quiet = True
    for lo, hi, text in edits.say:
        members = [position[i] for i in range(min(lo, hi), max(lo, hi) + 1) if i in position]
        if not members or not text or len(members) > SAY_MAX_BLOCKS or any(kept[m].group >= 0 for m in members):
            continue
        words = [len(kept[m].text) for m in members if kept[m].kind not in ("row", "img")]
        has_table = any(kept[m].kind == "row" for m in members)
        limit = SAY_MAX_BLOCK_CHARS if has_table else SAY_MAX_LINE_CHARS
        if any(n > limit for n in words):
            continue  # not data-like: those blocks are read as written
        for m in members:
            kept[m].group = members[0]
        kept[members[0]].say = text[:600]
    return kept


def _numbers(spec: str, count: int) -> set[int]:
    found: set[int] = set()
    for start, end in re.findall(r"(\d+)(?:\s*[-–]\s*(\d+))?", spec):
        lo, hi = int(start), int(end or start)
        found.update(i for i in range(min(lo, hi), max(lo, hi) + 1) if i < count)
    return found


def spoken_intro(mail: Email, title: str = "", source: str = "") -> str:
    title = title or clean_subject(mail.subject)
    # Emoji in subject lines get read out as their names ("hot beverage"), so drop them.
    title = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️‍]", "", title).strip()
    if not source and mail.message_id.startswith(inbox.PREFIX):
        return f"{title}."  # something you shared yourself: no "From Shared."
    return f"From {source or mail.sender}. {title}."


def _ask(client: anthropic.Anthropic, content: str) -> str:
    try:
        response = client.messages.create(
            model=os.environ.get("CLAUDE_MODEL") or DEFAULT_MODEL,
            max_tokens=4000,
            system=PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise SetupError(f"Claude rejected the API key: {exc}") from exc
    except anthropic.BadRequestError as exc:
        message = str(exc).lower()
        if "workspace" in message:
            raise SetupError(
                "This Claude API key isn't tied to a workspace. Create a new key inside a "
                "workspace at console.anthropic.com (Settings > API keys) and replace the "
                "ANTHROPIC_API_KEY secret, or set the ANTHROPIC_WORKSPACE_ID variable."
            ) from exc
        if "credit balance" in message:
            raise SetupError("Your Claude account is out of credit. Add some at console.anthropic.com.") from exc
        raise
    if response.stop_reason == "refusal":
        raise EditError("Claude declined to sort this email.")
    return "".join(block.text for block in response.content if block.type == "text")
