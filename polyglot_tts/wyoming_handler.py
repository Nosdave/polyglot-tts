"""Wyoming event handler for Pocket TTS — multi-language same-voice + sentence-streaming
+ HA-aware text-IN streaming (v1.3.1).

Architecture:
  - Multiple TTSModel-Instanzen pre-loaded by __main__ (one per language checkpoint).
  - Each custom voice encoded as N state-vectors (one per language) — same voice identity,
    different phonetic backbones.

Two synthesis paths handled in one handler:

  1. **Legacy single-shot** (back-compat, HA does this if `supports_synthesize_streaming=False`
     in our Info, OR if conversation-agent doesn't stream):
       Synthesize event → split on sentence boundaries → per-sentence synth+stream audio chunks.

  2. **HA-streaming-aware** (HA 2025.10+ pipeline with streaming conversation-agent):
       SynthesizeStart → SynthesizeChunk (per LLM token) → ... → SynthesizeStop.
       We buffer chunks, flush per detected sentence-boundary, synth+stream per sentence
       AS THEY ARRIVE. After SynthesizeStop we emit SynthesizeStopped — HA's read-loop
       waits for this terminator.

Voice-character notes: voice cloning preserves Timbre across languages but also carries
some Prosodie + native-language vowel-coloring. So Eve (English-source WAV) speaking
German keeps a subtle English Touch. Solution = use language-native source samples per
voice (e.g. recorded eve_de.wav). Architecture is correct, source-material choice matters.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

from pocket_tts import TTSModel
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
)

from .core import (
    ALL_PRESET_VOICES,
    BCP47_TO_CHECKPOINT,
    CHANNELS,
    LANGUAGE_TO_BCP47,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
)
from .text_norm import normalize as _normalize_text
from .timing_server import update_timing

_LOGGER = logging.getLogger(__name__)
# Smaller chunks help ESPHome `media_player` watermark heuristics: 1024 samples
# at 24 kHz = ~42 ms PCM per AudioChunk, vs. previous 170 ms. More frequent
# flushes give downstream players an earlier "enough buffer" signal. See
# https://github.com/esphome/feature-requests/issues/3148 for context.
CHUNK_SAMPLES = 1024

# v1.5.2 cleanup: `_extract_complete_sentences` (Multi-Tier-Regex) + `_split_sentences`
# (pysbd-Wrapper) wurden ersatzlos entfernt. Sie waren von der Filler-First-Strategie
# motiviert (kurze Sätze früh emittieren um HA's STREAM_RESPONSE_CHARS=60 Gate zu
# trippen), aber die LLM hat die Strategie nie zuverlässig befolgt. In v1.5.1 ist die
# Flush-Logik in `_maybe_start_synth` (text[-1] in ".!?…" + MIN_SYNTH_CHARS) ausreichend.
# Drop: pysbd-Dependency, Multi-Tier-Regex, _FALLBACK_SPLIT_RE, MIN_SOFT_FLUSH_LEN,
# HARD_FLUSH_LEN. Legacy-Synthesize-Pfad ruft jetzt _stream_frames direkt mit Volltext.


# v1.5.5 — Per-Model asyncio.Lock für `pad_with_spaces_for_short_inputs`-Mutation
# (Pocket-TTS-Doku Z. 547-548: „NOT thread-safe") + Generator-Serialisierung pro
# Modell-Instance über alle Handler-Instances hinweg. Key = id(model).
_GLOBAL_MODEL_LOCKS: dict[int, "asyncio.Lock"] = {}


def _get_model_lock(model) -> "asyncio.Lock":
    """Get-or-create per-model asyncio.Lock (lazy-init, module-global)."""
    key = id(model)
    lock = _GLOBAL_MODEL_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _GLOBAL_MODEL_LOCKS[key] = lock
    return lock


# ─── Lingua LID — built+cached once per process ─────────────────────────
# Lingua is much more reliable than py3langid for short text (uses 1-5-grams
# vs trigrams), AND returns calibrated confidence scores we can threshold.
# We restrict to the BCP47 codes we actually serve to massively boost accuracy.

_LINGUA_BCP47_TO_LANG: dict = {}    # lazy-init: {"de": Language.GERMAN, ...}
_LINGUA_DETECTORS: dict = {}        # lazy-init: {frozenset(["de","fr","en"]): detector}


def _get_lingua_detector(available: list[str]):
    """Build (or fetch cached) Lingua detector restricted to available BCP47-langs."""
    global _LINGUA_BCP47_TO_LANG
    try:
        from lingua import Language, LanguageDetectorBuilder
    except ImportError as e:
        _LOGGER.warning("lingua-language-detector not installed (%s); LID disabled", e)
        return None

    if not _LINGUA_BCP47_TO_LANG:
        _LINGUA_BCP47_TO_LANG = {
            "de": Language.GERMAN,
            "fr": Language.FRENCH,
            "en": Language.ENGLISH,
            "it": Language.ITALIAN,
            "es": Language.SPANISH,
            "pt": Language.PORTUGUESE,
        }

    key = frozenset(available)
    if key in _LINGUA_DETECTORS:
        return _LINGUA_DETECTORS[key]

    langs = [_LINGUA_BCP47_TO_LANG[c] for c in available if c in _LINGUA_BCP47_TO_LANG]
    if len(langs) < 2:
        _LOGGER.warning("Lingua needs >=2 languages, got %s; LID disabled", langs)
        return None
    detector = (
        LanguageDetectorBuilder.from_languages(*langs)
        .with_preloaded_language_models()
        .build()
    )
    _LINGUA_DETECTORS[key] = detector
    _LOGGER.info("Lingua detector built for langs=%s", available)
    return detector


def _detect_language(text: str, available: list[str], default: str) -> str:
    """Lingua-based Language Identification with confidence-gate.

    v1.5.3 — Simplified: das in v1.3.3 hinzugefügte Char-Pattern-Voting (Stopword-
    Regex über `der/die/das`, `le/la/les`, `the/is/are` etc.) wurde entfernt. Es
    war als Backup gedacht für py3langid-Misfires, ist aber für Lingua 2.0 mit
    `with_preloaded_language_models()` + Confidence-Threshold redundant. Empirie
    aus den v1.3.3-Tests bestätigte: Lingua erkennt DE/FR/EN-Smart-Home-Text bei
    ≥20 Zeichen zuverlässig (conf ≥0.55 + delta ≥0.10) ohne Heuristik-Voting.

    Algorithm:
      1. Build/fetch cached Lingua-detector restricted to `available` langs
      2. Get calibrated confidence values
      3. Accept top result only if `conf >= 0.55` AND `delta-to-#2 >= 0.10`
      4. Otherwise → `default`
    """
    text = (text or "").strip()
    if len(text) < 4:
        return default

    detector = _get_lingua_detector(available)
    if detector is None:
        return default

    try:
        confidences = detector.compute_language_confidence_values(text)
        if not confidences:
            return default
        top = confidences[0]
        top_conf = top.value
        top_bcp47 = top.language.iso_code_639_1.name.lower()
        delta = top_conf - (confidences[1].value if len(confidences) > 1 else 0.0)

        if top_conf >= 0.55 and delta >= 0.10 and top_bcp47 in available:
            _LOGGER.debug("Lingua LID: %s conf=%.2f delta=%.2f text=%r",
                          top_bcp47, top_conf, delta, text[:40])
            return top_bcp47
        _LOGGER.debug("LID uncertain (top=%s conf=%.2f delta=%.2f) → default=%s",
                      top_bcp47, top_conf, delta, default)
        return default
    except Exception as e:
        _LOGGER.debug("Lingua failed: %s — fallback %s", e, default)
        return default


class PocketTTSEventHandler(AsyncEventHandler):
    """Multi-language same-voice handler with HA-aware streaming text-IN."""

    def __init__(
        self,
        wyoming_info: Info,
        cli_args,
        models: dict,                       # {checkpoint_name: TTSModel}
        voice_states: dict,                 # {voice_name: {checkpoint_name: state}}
        advertised_bcp47: list[str],        # ["de", "fr", "en"]
        core: Any = None,                   # PolyglotCore — live voice registry
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.wyoming_info = wyoming_info
        self.cli_args = cli_args
        self.models = models
        self.voice_states = voice_states
        self.advertised_bcp47 = advertised_bcp47
        self._core = core
        self.default_checkpoint = next(iter(models.keys()))
        self.default_bcp47 = LANGUAGE_TO_BCP47.get(self.default_checkpoint.split("_")[0], "en")
        # bcp47 -> loaded checkpoint, from the actually-loaded models (handles
        # light variants like "german"); first-loaded wins per language.
        self._bcp47_to_checkpoint: dict[str, str] = {}
        for ckpt in models:
            bcp = LANGUAGE_TO_BCP47.get(ckpt.split("_")[0], ckpt[:2])
            self._bcp47_to_checkpoint.setdefault(bcp, ckpt)

        # ── streaming-session state (per connection) ──
        self._stream_active: bool = False
        self._stream_buffer: str = ""
        self._stream_voice_name: str | None = None
        self._stream_voice_lang: str | None = None     # BCP47 from SynthesizeStart.voice
        self._stream_resolved_ckpt: str | None = None  # cached after first sentence
        self._stream_audio_started: bool = False
        self._stream_t_start: float = 0.0
        self._stream_t_first_chunk: float | None = None
        self._stream_audio_bytes_total: int = 0
        self._stream_text_len: int = 0
        self._stream_sentences_emitted: int = 0

        # v1.5.4 — Drain-Loop statt Done-Callback-Cascade.
        # Ein langlaufender Background-Task pro Stream zieht Buffer-Stücke und
        # rendert sie sequentiell. Erweckt durch asyncio.Event bei jedem
        # neuen Chunk + bei SynthesizeStop. Vereinfacht v1.5.1's Cascade-State-
        # Machine (`_maybe_start_synth`+`_on_synth_task_done`+`_finalize_scheduled`)
        # zu einem konzeptionellen Modell. Verhalten identisch: nur 1 Generator
        # gleichzeitig, Buffer akkumuliert während Synth läuft, nächster Generator
        # bekommt was sich angesammelt hat.
        import os as _os_local
        self._min_synth_chars: int = int(_os_local.environ.get(
            "POCKET_TTS_MIN_SYNTH_CHARS", "30",
        ))
        self._stream_stop_received: bool = False
        self._drain_task: Any | None = None        # asyncio.Task — der Drain-Loop
        self._wake: Any | None = None              # asyncio.Event — erweckt Drain

    # ── helpers ───────────────────────────────────────────────────────────

    def _resolve_checkpoint(self, requested_bcp47: str | None, text: str) -> tuple[str, str]:
        """Pick (bcp47, checkpoint_name) — ElevenLabs-style on-the-fly multilingual.

        Priority:
          1. HA-provided voice.language hint (BCP47) — if present + valid
          2. Lingua-based LID on text (default-ON in v1.3.3+ since Lingua is reliable
             for short Smart-Home phrases when combined with character heuristics
             + confidence-thresholding). Disable per `POCKET_TTS_AUTO_LID=false`.
          3. Default checkpoint (first POCKET_TTS_LANGUAGES entry — user's primary).

        Notes:
          - HA core has a known bug: wyoming/tts.py builds SynthesizeVoice without
            language=, so step 1 rarely fires in practice. The architecture works
            anyway because LID adapts on-the-fly.
          - MIN_LID_CHARS=20 with Lingua is safe (was 80 with py3langid).
        """
        import os
        MIN_LID_CHARS = 20
        auto_lid_enabled = os.environ.get("POCKET_TTS_AUTO_LID", "true").lower() in ("1", "true", "yes")

        # Resolve via the loaded-models map (built from what's actually
        # loaded — handles light variants like "german"), not a hardcoded table.
        bcp_map = self._bcp47_to_checkpoint

        # 1. Explicit hint from HA Pipeline (preferred when actually provided)
        if requested_bcp47:
            bcp47 = requested_bcp47.split("-")[0].lower()
            ckpt = bcp_map.get(bcp47) if bcp_map else None
            if ckpt:
                _LOGGER.debug("Lang from hint: %s", bcp47)
                return bcp47, ckpt

        # 2. Lingua LID — on-the-fly multilingual (ElevenLabs-style)
        text_len = len((text or "").strip())
        if auto_lid_enabled and text_len >= MIN_LID_CHARS:
            bcp47 = _detect_language(text, self.advertised_bcp47, self.default_bcp47)
            ckpt = bcp_map.get(bcp47) if bcp_map else None
            if ckpt:
                _LOGGER.info("Lang via Lingua-LID (text_len=%d): %s", text_len, bcp47)
                return bcp47, ckpt

        # 3. Default fallback
        _LOGGER.info("Lang → default (text_len=%d, hint=%r, auto_lid=%s): %s/%s",
                     text_len, requested_bcp47, auto_lid_enabled,
                     self.default_bcp47, self.default_checkpoint)
        return self.default_bcp47, self.default_checkpoint

    def _get_voice_state(self, voice_name: str, checkpoint: str):
        per_lang = self.voice_states.get(voice_name)
        if per_lang and checkpoint in per_lang:
            return per_lang[checkpoint]
        if voice_name in ALL_PRESET_VOICES and checkpoint in self.models:
            try:
                _LOGGER.info("On-demand encode %s against %s", voice_name, checkpoint)
                state = self.models[checkpoint].get_state_for_audio_prompt(voice_name)  # type: ignore[arg-type]
                if voice_name not in self.voice_states:
                    self.voice_states[voice_name] = {}
                self.voice_states[voice_name][checkpoint] = state
                return state
            except Exception as e:
                _LOGGER.error("On-demand encoding failed for %s/%s: %s",
                              voice_name, checkpoint, e)
        return None

    async def _emit_audio_start_once(self) -> None:
        if not self._stream_audio_started:
            await self.write_event(AudioStart(
                rate=SAMPLE_RATE, width=SAMPLE_WIDTH, channels=CHANNELS,
            ).event())
            self._stream_audio_started = True

    # ── v1.5.4 — Drain-Loop ───────────────────────────────────────────────

    def _take_flushable_chunk(self) -> str:
        """Pull the next flushable text-chunk from `_stream_buffer`.

        Decision rules (1 conceptual model statt v1.5.1's 7 Cascade-Regeln):

        - Buffer leer  → "" (nichts zu tun)
        - SynthesizeStop empfangen → alles aus Buffer rausnehmen (Final-Flush)
        - Erster Synth des Streams (`_stream_sentences_emitted == 0`):
            flush nur wenn Buffer auf `.!?…` endet ODER ≥ MIN_SYNTH_CHARS
            (Default 30) → niedrige TTFA bei kurzen Filler-Antworten,
            sinnvolle Größe bei langen
        - Folge-Synths: alles aus Buffer nehmen, was sich während des
          vorherigen Generator-Laufs angesammelt hat (kein zweiter Mindest-
          Threshold → so wenige Generator-Restarts wie möglich)
        """
        text = self._stream_buffer.strip()
        if not text:
            return ""

        if self._stream_stop_received:
            # Final-Flush: alles raus
            self._stream_buffer = ""
            return text

        if self._stream_sentences_emitted == 0:
            # Erster Flush: warte auf Terminator ODER MIN_SYNTH_CHARS
            if text[-1] in ".!?…" or len(text) >= self._min_synth_chars:
                self._stream_buffer = ""
                return text
            return ""  # noch nicht genug

        # Folge-Flush: alles was sich während prev. Generator angesammelt hat
        self._stream_buffer = ""
        return text

    async def _drain_loop(self) -> None:
        """Background-Task pro Stream: zieht Chunks aus Buffer + rendert.

        Wartet auf `_wake`-Event (gesetzt bei jedem SynthesizeChunk + bei
        SynthesizeStop). Verarbeitet sequentiell — nur 1 Generator gleichzeitig.
        Terminiert wenn `_stream_stop_received` UND Buffer leer.
        """
        try:
            while True:
                # Warte bis ein Chunk reinkommt ODER Stop signalisiert wird
                await self._wake.wait()
                self._wake.clear()

                # Verarbeite alles was jetzt im Buffer flushable ist
                while True:
                    chunk = self._take_flushable_chunk()
                    if not chunk:
                        break
                    _LOGGER.info("Synth chunk start (chars=%d, last=%r, stop=%s)",
                                 len(chunk), chunk[-1] if chunk else "",
                                 self._stream_stop_received)
                    try:
                        await self._emit_audio_for_text(chunk)
                    except Exception as e:
                        _LOGGER.exception("Synth-error: %s", e)

                # Termination-Bedingung: Stop empfangen UND Buffer komplett leer
                if self._stream_stop_received and not self._stream_buffer.strip():
                    break
        finally:
            # Egal wie wir hier ankommen: finalize einmal aufrufen
            await self._finalize_stream()

    async def _emit_audio_for_text(self, text: str) -> None:
        """Synthesize TEXT (may contain multiple sentences) and stream frames."""
        # Resolve checkpoint once per stream (lazy on first synth)
        if self._stream_resolved_ckpt is None:
            bcp47, ckpt = self._resolve_checkpoint(self._stream_voice_lang, text)
            self._stream_resolved_ckpt = ckpt
            if not self._stream_voice_lang:
                self._stream_voice_lang = bcp47
            _LOGGER.info("Stream resolved: lang=%s ckpt=%s", bcp47, ckpt)

        model = self.models[self._stream_resolved_ckpt]
        state = self._get_voice_state(self._stream_voice_name, self._stream_resolved_ckpt)
        if state is None:
            _LOGGER.warning("Voice %r unavailable in %s — fallback to default %r",
                            self._stream_voice_name, self._stream_resolved_ckpt,
                            self.cli_args.voice)
            self._stream_voice_name = self.cli_args.voice
            state = self._get_voice_state(self._stream_voice_name, self._stream_resolved_ckpt)
        if state is None:
            _LOGGER.error("No voice state available — skipping synth of %r",
                          text[:40])
            return

        await self._stream_frames(model, state, text)

    async def _stream_frames(self, model, state, text: str) -> None:
        """Iterate Pocket-TTS' frame-streaming generator and emit AudioChunks.

        Single shared implementation for both streaming-IN path
        (_emit_audio_for_text via SynthesizeChunk/Stop buffering) and the
        legacy single-shot Synthesize path (called directly with full text).
        Each yielded Mimi-frame is split into Wyoming AudioChunks of
        CHUNK_SAMPLES samples (~42 ms @ 24 kHz mono 16-bit).

        v1.5.5 — Mukser-Fix für kurze Antworten:
        (a) `frames_after_eos=6` für Texte < 5 Wörter (default 5 zu wenig
            → FlowLM EOS-Frühzündung schneidet das letzte Wort ab)
        (b) `pad_with_spaces_for_short_inputs=True` temporär aktiviert für
            kurze Texte (8 Leerzeichen vor dem Text → mehr Token-Context)
        (c) Linear Fade-In auf das erste Mimi-Frame (120 Samples ~5 ms)
            um den ConvTranspose-Padding-Tail-Click (Issue #171) zu maskieren

        Verweis: kyutai-labs/pocket-tts tts_model.py:533-541 (`frames_after_eos`
        Kwarg), Z. 905-906 (`pad_with_spaces_for_short_inputs` Modell-attr),
        Z. 1027-1032 (per-Wort-Threshold). NICHT thread-safe für pad-Mutation
        — `_get_model_lock` serialisiert pro Modell.
        """
        import torch  # local — keep top imports clean
        loop = asyncio.get_running_loop()

        # v1.6.0 — Text-Normalization before synthesis:
        # Strip Markdown (** _ # links code), expand units (kWh→Kilowattstunden,
        # °C→Grad Celsius), convert numbers to words (23,5→dreiundzwanzig
        # Komma fünf). Sprachspezifisch über self._stream_voice_lang (de/en/fr,
        # fallback de). Toggle via env POCKET_TTS_TEXT_NORM=false.
        norm_lang = (self._stream_voice_lang or self.default_bcp47 or "de").split("-")[0]
        text_orig = text
        text = _normalize_text(text or "", lang=norm_lang)
        if text != text_orig:
            _LOGGER.debug(
                "Text-Norm [%s]: %r → %r",
                norm_lang, text_orig[:80], text[:120],
            )

        # Detect short text — apply mukser-fix
        word_count = len((text or "").split())
        is_short = word_count < 5

        # Build kwargs for generate_audio_stream
        stream_kwargs: dict = {}
        if is_short:
            stream_kwargs["frames_after_eos"] = 6  # default 3+2=5 was too short

        sentinel = object()
        fade_n = 120  # ~5 ms @ 24 kHz linear fade-in (masks Issue #171 click)

        # Serialize per-model — pad-mutation + concurrent-call safety.
        # Holding the lock for the whole generator iteration also enforces
        # "1 generator per model at a time" across handler-instances.
        async with _get_model_lock(model):
            old_pad = getattr(model, "pad_with_spaces_for_short_inputs", False)
            if is_short:
                model.pad_with_spaces_for_short_inputs = True

            try:
                gen = model.generate_audio_stream(state, text, **stream_kwargs)  # type: ignore[arg-type]
                first_chunk = True
                try:
                    while True:
                        pcm_chunk = await loop.run_in_executor(None, next, gen, sentinel)
                        if pcm_chunk is sentinel:
                            break

                        # pcm_chunk: torch.Tensor [n_samples], float32 in [-1, 1]
                        # First-Frame Fade-In gegen Mimi-ConvTranspose-Click
                        if first_chunk and pcm_chunk.numel() >= fade_n:
                            pcm_chunk = pcm_chunk.clone()
                            ramp = torch.linspace(
                                0.0, 1.0, fade_n,
                                device=pcm_chunk.device, dtype=pcm_chunk.dtype,
                            )
                            pcm_chunk[:fade_n] = pcm_chunk[:fade_n] * ramp
                            first_chunk = False

                        audio_np = pcm_chunk.detach().cpu().clamp(-1, 1).numpy()
                        audio_bytes = (audio_np * 32767).astype("int16").tobytes()

                        if self._stream_t_first_chunk is None:
                            self._stream_t_first_chunk = time.perf_counter()

                        chunk_size = CHUNK_SAMPLES * SAMPLE_WIDTH * CHANNELS
                        for i in range(0, len(audio_bytes), chunk_size):
                            await self.write_event(AudioChunk(
                                audio=audio_bytes[i:i + chunk_size],
                                rate=SAMPLE_RATE, width=SAMPLE_WIDTH, channels=CHANNELS,
                            ).event())
                        self._stream_audio_bytes_total += len(audio_bytes)
                finally:
                    try:
                        gen.close()
                    except Exception:
                        pass
            finally:
                # Restore pad state for next caller
                if is_short:
                    model.pad_with_spaces_for_short_inputs = old_pad

        self._stream_sentences_emitted += 1

    def _reset_stream_state(self) -> None:
        self._stream_active = False
        self._stream_buffer = ""
        self._stream_voice_name = None
        self._stream_voice_lang = None
        self._stream_resolved_ckpt = None
        self._stream_audio_started = False
        self._stream_t_start = 0.0
        self._stream_t_first_chunk = None
        self._stream_audio_bytes_total = 0
        self._stream_text_len = 0
        self._stream_sentences_emitted = 0
        # v1.5.4 reset
        self._stream_stop_received = False
        self._drain_task = None
        self._wake = None

    async def _finalize_stream(self) -> None:
        """End streaming session: AudioStop + SynthesizeStopped + timing-record."""
        await self.write_event(AudioStop().event())
        await self.write_event(SynthesizeStopped().event())  # ← terminator HA expects

        t_end = time.perf_counter()
        synth_ms = int((t_end - self._stream_t_start) * 1000) if self._stream_t_start else 0
        ttfa_ms = (
            int((self._stream_t_first_chunk - self._stream_t_start) * 1000)
            if self._stream_t_first_chunk and self._stream_t_start
            else synth_ms
        )
        audio_ms = int(self._stream_audio_bytes_total * 1000 / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS))
        update_timing(
            audio_ms=audio_ms,
            synth_ms=synth_ms,
            ttfa_ms=ttfa_ms,
            voice=self._stream_voice_name or self.cli_args.voice,
            language=self._stream_voice_lang or "?",
            text_len=self._stream_text_len,
        )
        _LOGGER.info("Stream done: voice=%s lang=%s sentences=%d audio=%dms synth=%dms ttfa=%dms text_chars=%d",
                     self._stream_voice_name, self._stream_voice_lang,
                     self._stream_sentences_emitted, audio_ms, synth_ms, ttfa_ms,
                     self._stream_text_len)
        self._reset_stream_state()

    # ── main event loop ───────────────────────────────────────────────────

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            # Rebuild the advertised voice list from the LIVE registry so a
            # voice cloned at runtime shows up on the next HA integration
            # refresh — without a restart. The startup-built self.wyoming_info
            # is only a fallback if no core was wired. core.voice_names() reads
            # under the registry lock (the file-watcher writes from a thread).
            if self._core is not None:
                info = get_wyoming_info(
                    self._core.voice_names(), self.advertised_bcp47
                )
            else:
                info = self.wyoming_info
            await self.write_event(info.event())
            return True

        # ── HA-Streaming path (Synthesize{Start,Chunk,Stop}) ──────────────

        if SynthesizeStart.is_type(event.type):
            ss = SynthesizeStart.from_event(event)
            self._reset_stream_state()
            self._stream_active = True
            self._stream_t_start = time.perf_counter()
            self._stream_voice_name = (
                ss.voice.name if (ss.voice and ss.voice.name) else self.cli_args.voice
            )
            self._stream_voice_lang = (
                ss.voice.language if (ss.voice and ss.voice.language) else None
            )
            # Emit AudioStart NOW so HA's player can start buffering early.
            # (All loaded checkpoints share 24kHz/16/mono — safe to commit upfront.)
            await self._emit_audio_start_once()
            # v1.5.4 — Drain-Loop starten (1 langlaufender Task pro Stream)
            self._wake = asyncio.Event()
            self._drain_task = asyncio.create_task(self._drain_loop())
            _LOGGER.info("SynthesizeStart received: voice=%s lang=%s",
                         self._stream_voice_name, self._stream_voice_lang)
            return True

        if SynthesizeChunk.is_type(event.type):
            sc = SynthesizeChunk.from_event(event)
            chunk_text = sc.text or ""
            if not self._stream_active:
                _LOGGER.warning("Got SynthesizeChunk without active stream — ignoring")
                return True
            # Per-chunk arrival logging — lets us see HA's actual chunking cadence
            # (progressive token-by-token vs. batched all-at-once). Relative-ms is
            # the most useful number to spot batching.
            t_arrival_ms = int((time.perf_counter() - self._stream_t_start) * 1000)
            _LOGGER.debug(
                "SynthesizeChunk +%dms (chars=%d, buf_before=%d): %r",
                t_arrival_ms, len(chunk_text), len(self._stream_buffer),
                chunk_text[:60] + ("…" if len(chunk_text) > 60 else ""),
            )
            self._stream_buffer += chunk_text
            self._stream_text_len += len(chunk_text)
            # v1.5.4 — Drain-Loop erwecken statt Cascade-Trigger
            if self._wake is not None:
                self._wake.set()
            return True

        if SynthesizeStop.is_type(event.type):
            if not self._stream_active:
                _LOGGER.warning("Got SynthesizeStop without active stream")
                return True
            _LOGGER.info("SynthesizeStop received (buf=%d chars, drain_running=%s)",
                         len(self._stream_buffer),
                         self._drain_task is not None and not self._drain_task.done())
            self._stream_stop_received = True
            # Drain-Loop erwecken; er sieht stop_received=True, flusht den Rest,
            # bricht aus seiner while-Schleife aus, ruft im finally _finalize_stream.
            if self._wake is not None:
                self._wake.set()
            return True

        # ── Legacy single-shot path (Synthesize event with full text) ─────

        if Synthesize.is_type(event.type):
            if self._stream_active:
                # HA's back-compat sends a final Synthesize after SynthesizeStop with the
                # accumulated text. We already streamed everything via chunks — skip it
                # to avoid double-synth. Spec note: this event arrives AFTER our final
                # SynthesizeStopped in some HA versions; in newer versions it may not
                # arrive at all if supports_synthesize_streaming=True.
                _LOGGER.debug("Ignoring back-compat Synthesize (stream session was active)")
                return True

            syn = Synthesize.from_event(event)
            text = (syn.text or "").strip()
            if not text:
                _LOGGER.warning("Empty Synthesize text — nothing to do")
                return True

            req_lang = syn.voice.language if (syn.voice and syn.voice.language) else None
            bcp47, checkpoint = self._resolve_checkpoint(req_lang, text)
            model = self.models[checkpoint]
            voice_name = self.cli_args.voice
            if syn.voice and syn.voice.name:
                voice_name = syn.voice.name
            elif syn.voice and getattr(syn.voice, "speaker", None):
                voice_name = syn.voice.speaker

            state = self._get_voice_state(voice_name, checkpoint)
            if state is None:
                _LOGGER.warning("Voice %r not available in %s — fallback to %r",
                                voice_name, checkpoint, self.cli_args.voice)
                voice_name = self.cli_args.voice
                state = self._get_voice_state(voice_name, checkpoint)
            if state is None:
                _LOGGER.error("No voice state available for legacy synth — aborting")
                return True

            _LOGGER.info("Legacy synth: voice=%s lang=%s (ckpt=%s) req_lang=%s text=%r",
                         voice_name, bcp47, checkpoint, req_lang, text[:80])

            # v1.5.2: kein Sentence-Split mehr — Pocket-TTS' generate_audio_stream
            # liefert intra-sentence frame-by-frame PCM, kein Aufgabe für uns vorher
            # zu zerteilen. Ein generate_audio_stream-Call pro Synthesize-Event.
            t_start = time.perf_counter()
            self._stream_t_first_chunk = None
            self._stream_audio_bytes_total = 0
            self._stream_sentences_emitted = 0

            await self.write_event(AudioStart(
                rate=SAMPLE_RATE, width=SAMPLE_WIDTH, channels=CHANNELS,
            ).event())

            try:
                await self._stream_frames(model, state, text)
            except Exception as e:
                _LOGGER.exception("Legacy generation error: %s", e)
            finally:
                await self.write_event(AudioStop().event())

            t_end = time.perf_counter()
            synth_ms = int((t_end - t_start) * 1000)
            ttfa_ms = (
                int((self._stream_t_first_chunk - t_start) * 1000)
                if self._stream_t_first_chunk else synth_ms
            )
            audio_ms = int(self._stream_audio_bytes_total * 1000 / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS))
            update_timing(
                audio_ms=audio_ms,
                synth_ms=synth_ms,
                ttfa_ms=ttfa_ms,
                voice=voice_name,
                language=bcp47,
                text_len=len(text),
            )
            _LOGGER.info("Legacy done: voice=%s lang=%s audio=%dms synth=%dms ttfa=%dms text_chars=%d",
                         voice_name, bcp47, audio_ms, synth_ms, ttfa_ms, len(text))
            # Reset stream-related counters (they were used as scratch in legacy too)
            self._stream_t_first_chunk = None
            self._stream_audio_bytes_total = 0
            self._stream_sentences_emitted = 0
            return True

        return True


# ── Wyoming Info advertising ─────────────────────────────────────────────

def get_wyoming_info(voices: list[str], bcp47_langs: list[str]) -> Info:
    """Advertise voices + signal we support streaming-IN (HA SynthesizeChunk events)."""
    tts_voices = []
    kyutai = Attribution(name="Kyutai", url="https://kyutai.org/")
    for v in voices:
        tts_voices.append(TtsVoice(
            name=v,
            attribution=kyutai,
            installed=True,
            description=f"Polyglot TTS voice: {v}",
            version=None,
            languages=list(bcp47_langs),
        ))
    from . import __version__
    return Info(
        tts=[TtsProgram(
            name="polyglot-tts",
            attribution=kyutai,
            installed=True,
            description=f"Polyglot TTS — multi-language, same voice, streaming ({','.join(bcp47_langs)})",
            version=__version__,
            voices=tts_voices,
            supports_synthesize_streaming=True,  # ← enables HA's text-IN streaming-mode
        )]
    )


def load_custom_voices(voices_dir: str, model: TTSModel) -> dict:
    """Legacy single-model loader (kept for backward import-compat)."""
    voice_states: dict = {}
    p = Path(voices_dir)
    if not p.exists():
        return voice_states
    audio_ext = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
    for f in p.iterdir():
        if f.suffix.lower() in audio_ext:
            try:
                voice_states[f.stem] = model.get_state_for_audio_prompt(str(f))  # type: ignore[arg-type]
            except Exception as e:
                _LOGGER.warning("Legacy loader failed for %s: %s", f, e)
    return voice_states
