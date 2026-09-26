"""Read Me: Gmail -> Claude (editor) -> Google voice -> podcast feed and reader.

Run it on a schedule (the GitHub Actions workflow runs it every hour). Each run:
  1. finds new emails with the ReadMe label or sent to you+readme@gmail.com,
  2. has Claude mark the clutter (footers, ads, "view in browser") to cut;
     the author's words are never rewritten,
  3. saves the article (text, links, photos) for the Read Me app,
  4. voices it right away if the sender is on your "always voice" list
     (everything else is voiced when you tap Listen in the app), and adds
     voiced emails to your podcast feed.

    python main.py            normal run
    python main.py --dry-run  print what would be read; no audio, no upload
"""

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import article
import editor
import mail
import podcast
import store as storage
import voice

GIVE_UP_AFTER = 3  # failed attempts before an email is skipped for good


def setting(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def run(fetch, edit, speak, store, *, now: datetime, max_emails: int,
        monthly_char_limit: int, keep_days: int, title: str) -> dict:
    """One pass over the inbox. The four callables are passed in so the tests
    can swap in fakes for Gmail, Claude, Google and the bucket."""
    first_run = store.read_json("state.json") is None
    state = podcast.load_state(store)
    processed = state["processed"]
    month = now.strftime("%Y-%m")
    always = set(storage.always_voice(store))
    summary = {"read": 0, "voiced": 0, "failed": 0, "not_worth_reading": 0,
               "over_budget": 0, "setup_error": ""}

    picked_up = _collect_on_demand_audio(state, store)
    if picked_up:
        print(f"Added {picked_up} episode(s) voiced from the app to the podcast feed.")
    swiped = _remove_swiped(state, store)
    if swiped:
        print(f"Removed {swiped} email(s) you swiped away in the app.")

    def waiting(m):
        entry = processed.get(m.message_id, {})
        return not (entry.get("status") in ("done", "not_worth_reading")
                    or entry.get("attempts", 0) >= GIVE_UP_AFTER)

    todo = [m for m in fetch() if waiting(m)][:max_emails]
    print(f"{len(todo)} new email(s) to read.")

    for m in todo:
        try:
            piece = edit(m)
            if piece is None:
                processed[m.message_id] = {"status": "not_worth_reading", "at": now.isoformat()}
                summary["not_worth_reading"] += 1
                print(f"  Skipped '{m.subject}': nothing worth reading (code, confirmation, etc.).")
                continue
            doc = article.reading_doc(piece.intro, piece.blocks)
            said, owners = article.segments(doc)
            chars = sum(len(x) for x in said)
            audio = b""
            if m.sender_address in always:
                used = state["usage"].get(month, 0)
                if used + chars > monthly_char_limit:
                    summary["over_budget"] += 1
                    print(f"  '{m.subject}' is ready to read, but voicing it would pass this month's "
                          f"free allowance ({monthly_char_limit:,} characters).")
                else:
                    audio, seconds = speak(said)
                    article.add_timings(doc, owners, seconds)
        except editor.SetupError as exc:
            # An account problem, not this email's fault: don't count it as a
            # failed try, and don't bother with the rest until it's fixed.
            summary["setup_error"] = str(exc)
            print(f"  Stopped: {exc}")
            break
        except Exception as exc:  # one bad email shouldn't stop the rest
            entry = processed.setdefault(m.message_id, {})
            attempts = entry.get("attempts", 0) + 1
            processed[m.message_id] = {"status": "failed", "attempts": attempts,
                                       "at": now.isoformat(), "error": str(exc)[:300]}
            print(f"  Couldn't read '{m.subject}' (try {attempts} of {GIVE_UP_AFTER}): {exc}")
            summary["failed"] += 1
            continue

        episode_id = hashlib.sha1(m.message_id.encode()).hexdigest()[:12]
        audio_path = f"audio/{episode_id}.mp3" if audio else ""
        text_path = f"text/{episode_id}.json"
        if audio:
            store.write(audio_path, audio, "audio/mpeg", cache=True)
            state["usage"][month] = state["usage"].get(month, 0) + chars
            summary["voiced"] += 1
        store.write(text_path, json.dumps(doc), "application/json")  # updated later if voiced from the app
        state["episodes"].append(asdict(podcast.Episode(
            id=episode_id,
            title=mail.clean_subject(m.subject),
            sender=m.sender,
            published=now.isoformat(),
            audio=audio_path,
            bytes=len(audio),
            chars=chars,
            text=text_path,
            sender_address=m.sender_address,
        )))
        if m.sender_address:
            state["senders"][m.sender_address] = {"name": m.sender, "last": now.isoformat()}
        processed[m.message_id] = {"status": "done", "at": now.isoformat()}
        podcast.publish(store, state, title)
        summary["read"] += 1
        print(f"  Ready: '{m.subject}' ({len(piece.blocks)} blocks, "
              f"{'voiced' if audio else 'voice on demand'}).")

    pruned = _forget_old(state, now, keep_days, store, always)
    # Quiet hours write nothing, which keeps storage operations in the free tier.
    if (first_run or pruned or picked_up or swiped or summary["failed"] or summary["not_worth_reading"]
            or summary["over_budget"] or summary["setup_error"]
            or podcast.assets_outdated(state, title)):
        podcast.publish(store, state, title)
    return summary


def _remove_swiped(state: dict, store) -> int:
    """Emails removed in the app: delete their files and drop them from the
    list and feed. They stay marked as read, so Gmail won't bring them back."""
    gone = set()
    for name in store.list("removed/"):
        marker = store.read_json(name) or {}
        if marker.get("id"):
            gone.add(marker["id"])
        store.delete(name)
    keep = []
    for ep in state["episodes"]:
        if ep["id"] in gone:
            for path in (ep.get("audio"), ep.get("text")):
                if path:
                    store.delete(path)
        else:
            keep.append(ep)
    count = len(state["episodes"]) - len(keep)
    state["episodes"] = keep
    return count


def _collect_on_demand_audio(state: dict, store) -> int:
    """Audio made from the app since the last run: attach it to its episode
    (so it joins the podcast feed) and count it against the month's allowance."""
    by_id = {ep["id"]: ep for ep in state["episodes"]}
    count = 0
    for name in store.list("voiced/"):
        marker = store.read_json(name) or {}
        ep = by_id.get(marker.get("id"))
        if ep is not None and not ep["audio"]:
            ep["audio"] = marker.get("audio", "")
            ep["bytes"] = int(marker.get("bytes", 0))
            count += 1
        if marker.get("month"):
            state["usage"][marker["month"]] = state["usage"].get(marker["month"], 0) + int(marker.get("chars", 0))
        store.delete(name)
    return count


def _forget_old(state: dict, now: datetime, keep_days: int, store, always=()) -> bool:
    """Delete episodes older than keep_days so storage stays inside the free tier.
    Returns True if any episode was removed."""
    cutoff = now - timedelta(days=keep_days)
    keep = []
    for ep in state["episodes"]:
        if datetime.fromisoformat(ep["published"]) < cutoff:
            if ep.get("audio"):
                store.delete(ep["audio"])
            if ep.get("text"):
                store.delete(ep["text"])
        else:
            keep.append(ep)
    pruned = len(keep) != len(state["episodes"])
    state["episodes"] = keep
    # Remember what's been read for a while longer than the Gmail lookback,
    # then let it go so state.json doesn't grow forever.
    forget = now - timedelta(days=max(keep_days, 60))
    state["processed"] = {
        k: v for k, v in state["processed"].items()
        if datetime.fromisoformat(v.get("at", now.isoformat())) >= forget
    }
    months = sorted(state["usage"])
    state["usage"] = {m: state["usage"][m] for m in months[-3:]}
    # Senders stay listed in the app's settings for 90 days after their last email.
    stale = (now - timedelta(days=90)).isoformat()
    state["senders"] = {a: v for a, v in state.get("senders", {}).items()
                        if a in always or v.get("last", "") >= stale}
    return pruned


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print what would be read; no audio or upload")
    args = parser.parse_args()

    missing = [n for n in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "ANTHROPIC_API_KEY") if not setting(n)]
    if not args.dry_run:
        missing += [n for n in ("GCS_BUCKET", "FEED_SECRET") if not setting(n)]
    if missing:
        print("Missing settings: " + ", ".join(missing) + ". See README.md for setup.")
        return 1

    def fetch():
        return mail.fetch_to_read(
            setting("GMAIL_ADDRESS"),
            setting("GMAIL_APP_PASSWORD").replace(" ", ""),
            setting("GMAIL_LABEL", "ReadMe"),
            setting("READING_ADDRESS") or mail.reading_address(setting("GMAIL_ADDRESS")),
            int(setting("LOOKBACK_DAYS", "7")),
        )

    if args.dry_run:
        for m in fetch()[: int(setting("MAX_EMAILS_PER_RUN", "10"))]:
            print(f"\n===== {m.subject} — {m.sender} =====\n")
            piece = editor.edit(m)
            if piece is None:
                print("(skipped: nothing worth reading)")
                continue
            print(piece.intro)
            for block in piece.blocks:
                print(f"[{block.kind}] {block.alt or block.src}" if block.kind == "img" else f"[{block.kind}] {block.text}")
        return 0

    store = storage.Bucket(setting("GCS_BUCKET"), setting("FEED_SECRET"))
    summary = run(
        fetch,
        editor.edit,
        lambda said: voice.synthesize(said, voice_name=storage.voice_choice(store)),
        store,
        now=datetime.now(timezone.utc),
        max_emails=int(setting("MAX_EMAILS_PER_RUN", "10")),
        monthly_char_limit=storage.monthly_limit(),
        keep_days=int(setting("KEEP_DAYS", "30")),
        title=setting("FEED_TITLE", "Read Me"),
    )
    print(f"\nDone: {summary['read']} ready ({summary['voiced']} voiced), {summary['failed']} failed.")
    print(f"Read Me app: {store.url('index.html')}")
    print(f"Podcast feed: {store.url('feed.xml')}")
    # Failed emails are retried next run, but mark this run red so GitHub
    # emails you that something needs a look.
    return 1 if summary["failed"] or summary["setup_error"] else 0


if __name__ == "__main__":
    sys.exit(main())
