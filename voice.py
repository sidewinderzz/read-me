"""Turn an article into an MP3 with Google Cloud Text-to-Speech.

Paragraphs are grouped into batches of about a minute and a half of speech,
the batches are voiced side by side, and the MP3s are joined end to end. That
keeps the wait short when you tap Listen. Each batch's length is measured and
shared out between its paragraphs by their length, which tells the app when
every paragraph starts so it can highlight along as it plays.
"""

import io
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from google.api_core import exceptions as google_errors
from google.cloud import texttospeech
from mutagen.mp3 import MP3

# Chirp 3 HD is Google's most natural voice family and includes 1 million free
# characters a month. Other names to try: en-US-Chirp3-HD-Aoede, -Puck, -Kore.
DEFAULT_VOICE = "en-US-Chirp3-HD-Charon"

MAX_BYTES = 4500  # under Google's 5,000-byte limit, with some headroom
BATCH_BYTES = 1500  # about 90 seconds of speech per request
PARALLEL = 6  # requests at once
PAUSE_WEIGHT = 20  # a paragraph break costs about as much time as 20 characters

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def synthesize(segments: list[str], client: texttospeech.TextToSpeechClient | None = None,
               voice_name: str | None = None) -> tuple[bytes, list[float]]:
    """One MP3 for all segments, plus how many seconds each segment lasts."""
    client = client or texttospeech.TextToSpeechClient()
    voice_name = voice_name or os.environ.get("TTS_VOICE") or DEFAULT_VOICE
    voice = texttospeech.VoiceSelectionParams(
        language_code="-".join(voice_name.split("-")[:2]),
        name=voice_name,
    )
    audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
    rate = float(os.environ.get("TTS_SPEAKING_RATE") or 1.0)
    if rate != 1.0:
        audio_config.speaking_rate = rate

    batches = batch(segments)

    def one(parts: list[tuple[int, str]]) -> tuple[bytes, float]:
        clip = _request(client, "\n\n".join(text for _, text in parts), voice, audio_config)
        return clip, mp3_seconds(clip)

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        clips = list(pool.map(one, batches))

    seconds = [0.0] * len(segments)
    for parts, (_, length) in zip(batches, clips):
        weights = [len(text) + PAUSE_WEIGHT for _, text in parts]
        for (owner, _), weight in zip(parts, weights):
            seconds[owner] += length * weight / sum(weights)
    return b"".join(clip for clip, _ in clips), seconds


def batch(segments: list[str], limit: int = BATCH_BYTES) -> list[list[tuple[int, str]]]:
    """Group segments, in order, into requests of about `limit` bytes. A segment
    longer than that is split at sentences into requests of its own."""
    batches: list[list[tuple[int, str]]] = []
    size = 0
    for owner, text in enumerate(segments):
        pieces = split_for_tts(text, max_bytes=limit) if _size(text) > limit else [text]
        for piece in pieces:
            if batches and size + _size(piece) + 2 <= limit:
                batches[-1].append((owner, piece))
                size += _size(piece) + 2
            else:
                batches.append([(owner, piece)])
                size = _size(piece)
    return batches


def mp3_seconds(clip: bytes) -> float:
    try:
        return float(MP3(io.BytesIO(clip)).info.length)
    except Exception:
        return len(clip) * 8 / 32_000  # Google's MP3s are 32 kbps


def _request(client, text, voice, audio_config) -> bytes:
    for attempt in range(6):
        try:
            response = client.synthesize_speech(
                input=texttospeech.SynthesisInput(text=text),
                voice=voice,
                audio_config=audio_config,
            )
            return response.audio_content
        except (google_errors.ResourceExhausted, google_errors.ServiceUnavailable):
            if attempt == 5:
                raise
            time.sleep(2 ** attempt)  # over the per-minute quota; wait and retry
    raise AssertionError("unreachable")


def split_for_tts(text: str, max_bytes: int = MAX_BYTES) -> list[str]:
    """Break text into pieces under max_bytes, splitting at paragraphs first,
    then sentences, then words, so the voice never stops mid-thought."""
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if _size(paragraph) <= max_bytes:
            pieces.append(paragraph)
            continue
        for sentence in _SENTENCE_END.split(paragraph):
            if _size(sentence) <= max_bytes:
                pieces.append(sentence)
            else:
                pieces.extend(_split_words(sentence, max_bytes))

    # Pack small pieces back together so we make as few requests as possible.
    chunks: list[str] = []
    for piece in pieces:
        if chunks and _size(chunks[-1] + "\n\n" + piece) <= max_bytes:
            chunks[-1] += "\n\n" + piece
        else:
            chunks.append(piece)
    return chunks


def _split_words(text: str, max_bytes: int) -> list[str]:
    out, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and _size(candidate) > max_bytes:
            out.append(current)
            current = word
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def _size(text: str) -> int:
    return len(text.encode("utf-8"))
