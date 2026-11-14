"""Streaming-Flush darf nie mitten im Wort schneiden (hörbare Pause „Dja… ner“)
und soll keine Ein-Wort-Synths erzeugen."""
from __future__ import annotations

import asyncio

from polyglot_tts.wyoming_handler import PocketTTSEventHandler, _mark_cut


def _handler(buf: str = "", emitted: int = 0, stop: bool = False,
             min_chars: int = 30, min_follow: int = 40):
    h = PocketTTSEventHandler.__new__(PocketTTSEventHandler)
    h._stream_buffer = buf
    h._stream_sentences_emitted = emitted
    h._stream_stop_received = stop
    h._min_synth_chars = min_chars
    h._min_follow_chars = min_follow
    return h


def test_mark_cut_prefers_sentence_then_clause():
    assert _mark_cut("Hallo Welt. Dein nächster Ter") == len("Hallo Welt.")
    assert _mark_cut("Dein Termin ist morgen, um 17 Uhr bei Dja") == len("Dein Termin ist morgen,")
    assert _mark_cut("Dein nächster Barber Ter") == 0


def test_ordinals_and_abbreviations_are_not_sentence_ends():
    assert _mark_cut("Der Termin ist am 27. Oktober um sieb") == 0
    assert _mark_cut("Termin bei Dr. Fraenkel um zehn") == 0
    assert _mark_cut("Zum Beispiel z. B. Brot und Milch") == 0
    assert _mark_cut("Das ist erledigt. Und dann") == len("Das ist erledigt.")


def test_french_nbsp_before_punctuation():
    t = "C'est fait ! Et ensuite on"
    assert _mark_cut(t) == len("C'est fait !")


def test_first_flush_keeps_partial_word_and_whitespace():
    h = _handler("Dein nächster Barber-Termin ist morgen bei Dja")
    assert h._take_flushable_chunk() == "Dein nächster Barber-Termin ist morgen bei"
    assert h._stream_buffer == " Dja"


def test_trailing_space_of_leftover_is_kept():
    h = _handler("Hallo liebe Welt da draußen, wie ", emitted=1)
    assert h._take_flushable_chunk() == "Hallo liebe Welt da draußen,"
    assert h._stream_buffer == " wie "


def test_short_clause_is_not_a_chunk_of_its_own():
    h = _handler(" Dienstag, um 17", emitted=1)
    assert h._take_flushable_chunk() == ""


def test_first_flush_waits_below_min_chars():
    h = _handler("Dein nächster Ter")
    assert h._take_flushable_chunk() == ""
    assert h._stream_buffer == "Dein nächster Ter"


def test_sentence_end_flushes_whole_buffer():
    h = _handler("Lift kommt.")
    assert h._take_flushable_chunk() == "Lift kommt."
    assert h._stream_buffer == ""


def test_follow_up_without_mark_waits_for_min_follow():
    h = _handler(" um 17 Uhr bei Dja", emitted=1)
    assert h._take_flushable_chunk() == ""
    h = _handler(" und dann gehen wir noch gemeinsam zum Bäcker an der Ec", emitted=1)
    assert h._take_flushable_chunk() == "und dann gehen wir noch gemeinsam zum Bäcker an der"


def test_one_drain_pass_does_not_split_mid_clause():
    h = _handler("Satz eins ist fertig. Und dann noch viele Wörter hier oh", emitted=1)
    assert h._take_flushable_chunk() == "Satz eins ist fertig."
    assert h._take_flushable_chunk() == ""  # Rest < MIN_FOLLOW, keine Marke → warten


def test_stop_flushes_everything():
    h = _handler(" Djaner in Uccle", emitted=1, stop=True)
    assert h._take_flushable_chunk() == "Djaner in Uccle"
    assert h._stream_buffer == ""


def test_drain_loop_token_stream_never_cuts_words():
    """Token-für-Token-Stream wie vom LLM; Stop kommt während eines Synths."""
    tokens = ["Dein", " nächster", " Barber", "-Ter", "min", " ist", " morgen", ",",
              " Dienstag", ",", " um", " 17", " Uhr", " bei", " Dja", "ner", " in",
              " Uc", "cle", "."]
    full = "".join(tokens)

    async def run():
        h = _handler(min_chars=30, min_follow=40)
        h._wake = asyncio.Event()
        chunks: list[str] = []

        async def emit(text):
            chunks.append(text)
            h._stream_sentences_emitted += 1
            await asyncio.sleep(0)  # Synth „läuft“, Tokens kommen weiter

        async def finalize():
            pass

        h._emit_audio_for_text = emit
        h._finalize_stream = finalize
        task = asyncio.create_task(h._drain_loop())
        for tok in tokens:
            h._stream_buffer += tok
            h._wake.set()
            await asyncio.sleep(0)
        h._stream_stop_received = True
        h._wake.set()
        await asyncio.wait_for(task, 2)
        return chunks

    chunks = asyncio.run(run())
    assert " ".join(chunks) == full
    words = set(full.replace(",", " ").replace(".", " ").split())
    for c in chunks:
        assert c.split()[0].strip(",.") in words and c.split()[-1].strip(",.") in words
    assert all(len(c.split()) >= 2 for c in chunks[:-1])


def _drain(tokens, min_chars=30, min_follow=40):
    async def run():
        h = _handler(min_chars=min_chars, min_follow=min_follow)
        h._wake = asyncio.Event()
        chunks: list[str] = []

        async def emit(text):
            chunks.append(text)
            h._stream_sentences_emitted += 1
            await asyncio.sleep(0)

        async def finalize():
            pass

        h._emit_audio_for_text = emit
        h._finalize_stream = finalize
        task = asyncio.create_task(h._drain_loop())
        for tok in tokens:
            h._stream_buffer += tok
            h._wake.set()
            await asyncio.sleep(0)
        h._stream_stop_received = True
        h._wake.set()
        await asyncio.wait_for(task, 2)
        return chunks

    return asyncio.run(run())


def test_dot_token_after_number_or_abbreviation_is_no_cut():
    toks = ["Ihr", " nächster", " Termin", " ist", " am", " 27", ".", " Oktober", " um", " zehn",
            " Uhr", " bei", " Dr", ".", " Fraenkel", " in", " Uccle", "."]
    chunks = _drain(toks)
    assert not any(c.endswith(("27.", "Dr.")) for c in chunks), chunks
    assert " ".join(chunks) == "".join(toks)
    toks = ["It", " is", " 23", ".", "5", " degrees", " outside", " right", " now", "."]
    chunks = _drain(toks, min_chars=10)
    assert not any(c.endswith("23.") for c in chunks), chunks
