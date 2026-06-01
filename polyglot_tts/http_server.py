"""OpenAI-Speech-compatible HTTP endpoint + Voice-Management REST API.

Implements the subset of the OpenAI /v1/audio/speech API that conversational
clients (OpenClaw, LangChain, custom scripts) actually use. The handler runs
synthesis through the same PolyglotCore the Wyoming endpoint uses — same
voices, same LID, same Mimi decoder, same text-normalization.

Endpoints
---------
GET  /health
GET  /v1/audio/voices
POST /v1/audio/voices            multipart: file=<wav>, name=<name>
DELETE /v1/audio/voices/{name}
GET  /v1/audio/languages
POST /v1/audio/speech            JSON: {model, input, voice, response_format,
                                       language?, instructions?}

Request body shape mirrors OpenAI's API:
    {
        "model":  "polyglot-1",
        "input":  "Bonjour le monde",
        "voice":  "eve",
        "response_format": "mp3"   // or "wav", "opus", "flac", "pcm"
    }

`response_format=mp3|opus|flac` is transcoded from raw PCM with soundfile/
ffmpeg fallback. `wav` and `pcm` skip transcoding.

Streaming the response body is supported via chunked-transfer; clients may
read audio as it's synthesized.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__
from .core import (
    BCP47_TO_CHECKPOINT,
    CHANNELS,
    LANGUAGE_TO_BCP47,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    PolyglotCore,
    auto_lid_enabled,
)
from .text_norm import normalize as normalize_text
from .timing_server import update_timing

_LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Request / response models
# ─────────────────────────────────────────────────────────────────────────

MAX_INPUT_CHARS = 4000        # cap synth input — prevents GPU/CPU DoS
MAX_VOICE_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MB hard cap for voice uploads


class SpeechRequest(BaseModel):
    model: str = Field(default="polyglot-1")
    input: str = Field(..., max_length=MAX_INPUT_CHARS)
    voice: str | None = None
    response_format: str = Field(default="mp3")
    # Polyglot-specific extension: explicit language hint
    language: str | None = None
    # Per-request sampling temperature (0.1–1.5). Overrides the global value for
    # this one call only; clamped to bounds. Omit to use the configured global.
    temperature: float | None = None
    # OpenAI fields we accept but currently ignore (kept for client compat)
    speed: float | None = None
    instructions: str | None = None


class NormalizeRequest(BaseModel):
    input: str = Field(..., max_length=MAX_INPUT_CHARS)
    language: str | None = None


# Voice-name validation: allow [A-Za-z0-9_-.] only, no leading dot, no ".."
_VOICE_NAME_OK = lambda s: (
    bool(s)
    and not s.startswith(".")
    and ".." not in s
    and all(c.isalnum() or c in "_-." for c in s)
)


# ─────────────────────────────────────────────────────────────────────────
# Lingua LID — reused from wyoming_handler module
# ─────────────────────────────────────────────────────────────────────────

def _detect_language(text: str, available: list[str], default: str) -> str:
    from .wyoming_handler import _detect_language as _impl
    return _impl(text, available, default)


# ─────────────────────────────────────────────────────────────────────────
# Synthesis core (HTTP path)
# ─────────────────────────────────────────────────────────────────────────

def _resolve_checkpoint(core: PolyglotCore, lang_hint: str | None,
                        text: str) -> tuple[str, str]:
    """Pick (bcp47, checkpoint). Mirrors handler._resolve_checkpoint."""
    MIN_LID_CHARS = 20
    # Resolve via the loaded-models map (handles light variants like "german"),
    # not a hardcoded table.
    if lang_hint:
        bcp47 = lang_hint.split("-")[0].lower()
        ckpt = core.bcp47_to_checkpoint.get(bcp47)
        if ckpt:
            return bcp47, ckpt
    text_len = len((text or "").strip())
    if auto_lid_enabled() and text_len >= MIN_LID_CHARS:
        bcp47 = _detect_language(text, core.advertised_bcp47, core.default_bcp47)
        ckpt = core.bcp47_to_checkpoint.get(bcp47)
        if ckpt:
            return bcp47, ckpt
    return core.default_bcp47, core.default_checkpoint


def _synthesize_pcm(core: PolyglotCore, voice: str, text: str,
                    lang_hint: str | None,
                    temperature: float | None = None) -> tuple[np.ndarray, str]:
    """Run model inference end-to-end, return (float32 mono samples @ 24kHz, lang).

    Acquires the shared per-model lock around `generate_audio_stream` so two
    concurrent HTTP synth requests (or HTTP + Wyoming via its own offload)
    don't corrupt pocket-tts's per-model mutable state (Issue tracked in
    pocket-tts:tts_model.py:547-548 — `pad_with_spaces_for_short_inputs` is
    explicitly NOT thread-safe).
    """
    bcp47, ckpt = _resolve_checkpoint(core, lang_hint, text)
    model = core.models[ckpt]
    state = core.get_voice_state(voice, ckpt)
    if state is None:
        # Fall back to default voice
        _LOGGER.info("Voice %r unavailable for %s — falling back to %r",
                     voice, ckpt, core.default_voice)
        state = core.get_voice_state(core.default_voice, ckpt)
        if state is None:
            # Last-ditch: encode the preset voice on the fly. Guard with
            # try/except — pocket-tts raises on unknown preset names rather
            # than returning None, and we want a 404 not a 500.
            from .core import ALL_PRESET_VOICES
            if voice in ALL_PRESET_VOICES:
                _LOGGER.info("On-demand encode of preset %s", voice)
                try:
                    state = model.get_state_for_audio_prompt(voice)
                except Exception as e:
                    _LOGGER.warning("On-demand encode of %r failed: %s", voice, e)
                    state = None
                if state is not None:
                    core.set_voice_state(voice, ckpt, state)
            if state is None:
                raise HTTPException(404, f"Voice '{voice}' not available")

    text_norm = normalize_text(text, lang=bcp47)
    _LOGGER.info("HTTP synth: voice=%s lang=%s ckpt=%s chars=%d",
                 voice, bcp47, ckpt, len(text_norm))

    # Per-request temperature: override model.temp for this synthesis only,
    # restore the global afterwards. Safe because we hold the model lock for the
    # whole generation, so concurrent requests with different temps serialize.
    from .core import TEMP_MAX, TEMP_MIN
    override_temp = None
    if temperature is not None:
        override_temp = min(max(float(temperature), TEMP_MIN), TEMP_MAX)

    t0 = time.perf_counter()
    pcm_chunks: list[np.ndarray] = []
    model_lock = core.get_model_lock(model)
    with model_lock:
        prev_temp = getattr(model, "temp", None)
        if override_temp is not None:
            model.temp = override_temp
        try:
            for frame in model.generate_audio_stream(state, text_norm):
                if hasattr(frame, "cpu"):
                    frame = frame.cpu().numpy()
                pcm_chunks.append(np.asarray(frame, dtype=np.float32).reshape(-1))
        finally:
            if override_temp is not None and prev_temp is not None:
                model.temp = prev_temp
    pcm = np.concatenate(pcm_chunks) if pcm_chunks else np.zeros(0, dtype=np.float32)
    synth_ms = int((time.perf_counter() - t0) * 1000)
    audio_ms = int(len(pcm) / SAMPLE_RATE * 1000)

    update_timing(
        audio_ms=audio_ms, synth_ms=synth_ms, ttfa_ms=0,
        voice=voice, language=bcp47, text_len=len(text),
    )
    return pcm, bcp47


def _encode_audio(pcm_f32: np.ndarray, fmt: str) -> tuple[bytes, str]:
    """Encode float32 PCM @ SAMPLE_RATE into the requested format.

    Returns (bytes, content-type). Supported: mp3, wav, flac, opus, pcm.
    """
    fmt = fmt.lower()
    if fmt == "pcm":
        # Raw 16-bit signed little-endian @ SAMPLE_RATE — for low-latency clients
        i16 = (np.clip(pcm_f32, -1.0, 1.0) * 32767).astype(np.int16)
        return i16.tobytes(), "audio/pcm"

    buf = io.BytesIO()
    if fmt == "wav":
        sf.write(buf, pcm_f32, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        return buf.getvalue(), "audio/wav"
    if fmt == "flac":
        sf.write(buf, pcm_f32, SAMPLE_RATE, format="FLAC")
        return buf.getvalue(), "audio/flac"
    if fmt == "opus":
        # soundfile supports OGG/OPUS if libsndfile >= 1.0.29
        try:
            sf.write(buf, pcm_f32, SAMPLE_RATE, format="OGG", subtype="OPUS")
            return buf.getvalue(), "audio/ogg"
        except Exception as e:
            _LOGGER.warning("opus encoding failed (%s), falling back to wav", e)
            buf = io.BytesIO()
            sf.write(buf, pcm_f32, SAMPLE_RATE, format="WAV", subtype="PCM_16")
            return buf.getvalue(), "audio/wav"
    # default = mp3
    try:
        sf.write(buf, pcm_f32, SAMPLE_RATE, format="MP3")
        return buf.getvalue(), "audio/mpeg"
    except Exception:
        # libsndfile may lack MP3 — degrade to wav
        _LOGGER.warning("mp3 encoding not available, returning wav")
        buf = io.BytesIO()
        sf.write(buf, pcm_f32, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        return buf.getvalue(), "audio/wav"


# ─────────────────────────────────────────────────────────────────────────
# FastAPI app factory
# ─────────────────────────────────────────────────────────────────────────

def build_app(core: PolyglotCore, voices_extra_dir: Path | None) -> FastAPI:
    app = FastAPI(
        title="Polyglot TTS",
        version=__version__,
        description="OpenAI-Speech-compatible HTTP endpoint for Polyglot TTS.",
    )

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "languages_loaded": list(core.models.keys()),
            "voices_loaded": len(core.voice_names()),
        }

    @app.get("/v1/audio/languages")
    async def list_languages() -> dict:
        return {
            "loaded": [
                {"checkpoint": ckpt,
                 "bcp47": LANGUAGE_TO_BCP47.get(ckpt.split("_")[0], "??")}
                for ckpt in core.models.keys()
            ],
            "default_bcp47": core.default_bcp47,
        }

    @app.get("/v1/audio/voices")
    async def list_voices() -> dict:
        return {"voices": core.voice_info()}

    @app.post("/v1/audio/voices", status_code=201)
    async def add_voice(
        file: UploadFile = File(...),
        name: str | None = Form(None),
    ) -> dict:
        if voices_extra_dir is None:
            raise HTTPException(503, "Voice management is disabled "
                                     "(voices-extra mount not configured)")
        # Determine target name
        if not name:
            stem = Path(file.filename or "").stem
            if not stem:
                raise HTTPException(400, "No voice name provided")
            name = stem
        # Strict name validation: no path-traversal, no leading dot.
        if not _VOICE_NAME_OK(name):
            raise HTTPException(400, "Invalid voice name "
                                     "(allowed: A-Z, a-z, 0-9, _, -, .; "
                                     "no leading dot; no '..')")
        # Stream upload to disk with a hard size cap. Doesn't buffer the
        # whole payload in RAM — protects 4 GB hosts (HA Green / Pi).
        suffix = Path(file.filename or ".wav").suffix.lower() or ".wav"
        target = voices_extra_dir / f"{name}{suffix}"
        # Resolve final path is under voices-extra (belt-and-braces vs symlinks)
        try:
            resolved = target.resolve()
            voices_extra_resolved = voices_extra_dir.resolve()
            resolved.relative_to(voices_extra_resolved)
        except (ValueError, OSError):
            raise HTTPException(400, "Resolved path escapes voices-extra/")

        total = 0
        CHUNK = 1024 * 1024
        try:
            with open(target, "wb") as fh:
                while True:
                    data = await file.read(CHUNK)
                    if not data:
                        break
                    total += len(data)
                    if total > MAX_VOICE_UPLOAD_BYTES:
                        fh.close()
                        target.unlink(missing_ok=True)
                        raise HTTPException(
                            413,
                            f"Voice file too large "
                            f"(max {MAX_VOICE_UPLOAD_BYTES // (1024*1024)} MB)",
                        )
                    fh.write(data)
        except HTTPException:
            raise
        except Exception:
            target.unlink(missing_ok=True)
            raise HTTPException(500, "Upload failed")

        return {"name": name, "path": str(target),
                "status": "queued — embedding will start in <2s"}

    @app.delete("/v1/audio/voices/{name}", status_code=204)
    async def delete_voice(name: str) -> Response:
        if voices_extra_dir is None:
            raise HTTPException(503, "Voice management is disabled")
        # Same strict validation as upload — block path-traversal.
        if not _VOICE_NAME_OK(name):
            raise HTTPException(400, "Invalid voice name")
        # Remove from registry immediately
        core.remove_voice(name)
        # Delete source file(s) if present — watcher will also catch this.
        # Iterate over AUDIO_EXT for consistency with the upload path.
        from .voice_loader import AUDIO_EXT
        for suffix in AUDIO_EXT:
            p = voices_extra_dir / f"{name}{suffix}"
            if p.exists():
                # Final safety: don't unlink a symlink that points outside.
                try:
                    p.resolve().relative_to(voices_extra_dir.resolve())
                except (ValueError, OSError):
                    continue
                p.unlink()
        return Response(status_code=204)

    @app.post("/v1/audio/speech")
    async def synthesize(req: SpeechRequest) -> Response:
        voice = req.voice or core.default_voice
        # Run synthesis in a thread — model.generate is blocking.
        try:
            pcm, lang = await asyncio.to_thread(
                _synthesize_pcm, core, voice, req.input, req.language,
                req.temperature,
            )
        except HTTPException:
            raise
        except Exception as e:
            _LOGGER.exception("Synthesis failed: %s", e)
            # Don't leak internal exception detail to clients.
            raise HTTPException(500, "Synthesis failed") from e
        audio_bytes, content_type = _encode_audio(pcm, req.response_format)
        return Response(
            content=audio_bytes,
            media_type=content_type,
            headers={
                "X-Polyglot-Language": lang,
                "X-Polyglot-Voice": voice,
            },
        )

    @app.post("/v1/text/normalize")
    async def normalize_endpoint(req: NormalizeRequest) -> dict:
        """Preview the text-normalization pipeline (numbers, units, ordinals,
        Markdown strip) for a given text + language. Useful for tuning what
        the synthesizer will actually speak."""
        lang = (req.language or core.default_bcp47 or "de").split("-")[0]
        return {
            "input": req.input,
            "language": lang,
            "normalized": normalize_text(req.input, lang=lang),
        }

    @app.get("/")
    async def root() -> dict:
        return {
            "name": "polyglot-tts",
            "version": __version__,
            "endpoints": {
                "openai_speech":  "POST /v1/audio/speech",
                "voices_list":    "GET /v1/audio/voices",
                "voices_add":     "POST /v1/audio/voices",
                "voices_delete":  "DELETE /v1/audio/voices/{name}",
                "languages":      "GET /v1/audio/languages",
                "text_normalize": "POST /v1/text/normalize",
                "health":         "GET /health",
            },
        }

    return app
