from voice import split_for_tts


def _size(s):
    return len(s.encode())


def test_short_text_is_one_chunk():
    assert split_for_tts("Hello there.\n\nSecond paragraph.") == ["Hello there.\n\nSecond paragraph."]


def test_long_text_splits_under_limit_without_losing_words():
    para = " ".join(f"Sentence number {i} is here." for i in range(400))
    text = "\n\n".join([para, "Short one.", para])
    chunks = split_for_tts(text, max_bytes=1000)
    assert len(chunks) > 1
    assert all(_size(c) <= 1000 for c in chunks)
    assert " ".join(" ".join(chunks).split()) == " ".join(text.split())


def test_giant_sentence_splits_on_words():
    text = "word " * 1000
    chunks = split_for_tts(text, max_bytes=500)
    assert all(_size(c) <= 500 for c in chunks)
    assert sum(c.count("word") for c in chunks) == 1000


def test_multibyte_characters_counted_as_bytes():
    text = "é" * 600  # 1200 bytes, no spaces
    text = " ".join([text[:300], text[300:]])
    chunks = split_for_tts(text, max_bytes=700)
    assert all(_size(c) <= 700 for c in chunks)


def test_batches_keep_order_and_size():
    import voice
    segs = ["a" * 600, "b" * 600, "c" * 600, "Word here. " * 300]
    batches = voice.batch(segs, limit=1500)
    assert [[owner for owner, _ in b] for b in batches][:2] == [[0, 1], [2]]
    assert all(sum(len(t) + 2 for _, t in b) <= 1502 for b in batches)
    assert " ".join(t for b in batches for o, t in b if o == 3).split() == ("Word here. " * 300).split()


def test_synthesize_shares_batch_time_by_length():
    import voice

    class Client:
        def __init__(self):
            self.texts = []

        def synthesize_speech(self, input, voice, audio_config):
            self.texts.append(input.text)
            return type("R", (), {"audio_content": b"x" * 4000})()  # 1 second at 32 kbps

    client = Client()
    audio, seconds = voice.synthesize(["Intro.", "A much longer first paragraph here."], client=client)
    assert client.texts == ["Intro.\n\nA much longer first paragraph here."]  # one request
    assert audio == b"x" * 4000
    assert abs(sum(seconds) - 1.0) < 1e-9
    assert seconds[1] > seconds[0]
