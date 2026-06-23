"""Dispatcher — env-driven launcher.

Reads environment variables, loads TTS models + voices, builds a shared
PolyglotCore, and starts every endpoint requested.

Environment variables
---------------------
POCKET_TTS_LANGUAGES        Comma-separated Pocket-TTS language checkpoints
                            (default: english_2026-04,german_24l,french_24l)
POCKET_TTS_VOICE            Default voice when none specified per request
                            (default: eve)
POCKET_TTS_VOICES_DIR       Built-in voices dir, baked into image
                            (default: /app/voices)
POCKET_TTS_VOICES_EXTRA_DIR User-mounted voices dir; file-watcher watches this
                            (default: /app/voices-extra)
POCKET_TTS_WYOMING_PORT     TCP port for Wyoming endpoint (default: 10200,
                            set to "" to disable)
POCKET_TTS_HTTP_PORT        TCP port for OpenAI-Speech HTTP endpoint
                            (default: 10201, set to "" to disable)
POCKET_TTS_TIMING_PORT      TCP port for side-channel timing endpoint
                            (default: 10299)
POCKET_TTS_HOST             Bind address (default: 0.0.0.0)
POCKET_TTS_DEVICE           "auto" (default) | "cpu" | "cuda"
POCKET_TTS_WARMUP           "true" (default) | "false"
POCKET_TTS_TEXT_NORM        "true" (default) | "false"
POCKET_TTS_AUTO_LID         "true" (default) | "false"
POCKET_TTS_LAZY_LOAD        "false" (default) | "true" — load missing langs
                            on first request (experimental)
POCKET_TTS_MIN_SYNTH_CHARS  Streaming first-flush threshold (default: 30)
HF_TOKEN                    HuggingFace token for gated models (optional)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from .core import LANGUAGE_TO_BCP47, PolyglotCore
from .timing_server import start_timing_server
from .voice_loader import load_initial_voices
from .voice_watcher import start_watcher

_LOGGER = logging.getLogger(__name__)


def _bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes")


def _int_env(name: str, default: int | None) -> int | None:
    val = os.environ.get(name)
    if val is None:
        return default
    if val == "":
        return None
    try:
        return int(val)
    except ValueError:
        _LOGGER.warning("Invalid int for %s=%r, using default=%r",
                        name, val, default)
        return default


def _resolve_temp() -> float:
    """Initial sampling temperature from POCKET_TTS_TEMP, used at model load.

    This only sets the starting value — `model.temp` is read live at each decode
    step, so it can be changed afterwards globally (UI/config) or per request
    (HTTP), without a reload. Clamped to [0.1, 1.5]; bad/empty input → 0.7.
    """
    from .core import clamp_temperature
    return clamp_temperature(os.environ.get("POCKET_TTS_TEMP", "").strip() or None)


def _load_models(checkpoints: list[str], device_pref: str) -> dict:
    """Load each requested checkpoint. Returns dict {ckpt: TTSModel}."""
    from pocket_tts import TTSModel

    temp = _resolve_temp()
    _LOGGER.info("Sampling temperature (global, load-time): %.2f", temp)
    models: dict = {}
    for ckpt in checkpoints:
        try:
            _LOGGER.info("Loading checkpoint: %s ...", ckpt)
            t0 = time.time()
            m = TTSModel.load_model(language=ckpt, temp=temp)
            dt = time.time() - t0
            _LOGGER.info("  loaded in %.1fs, sample_rate=%d Hz", dt, m.sample_rate)
            models[ckpt] = m
        except Exception as e:
            _LOGGER.exception("FAILED to load checkpoint %s: %s", ckpt, e)
            continue

    if not models:
        raise SystemExit("No language checkpoints loaded — aborting.")

    # Device placement
    import torch
    if device_pref == "cuda" or (device_pref == "auto" and torch.cuda.is_available()):
        if not torch.cuda.is_available():
            _LOGGER.warning("CUDA requested but not available — staying on CPU")
        else:
            dev = torch.device("cuda")
            _LOGGER.info("Moving %d model(s) to GPU (%s)",
                         len(models), torch.cuda.get_device_name(0))
            import gc
            for ckpt in list(models.keys()):
                models[ckpt] = models[ckpt].to(dev)
                # Release the CPU→GPU transients of this model before moving the
                # next one — caps the cumulative load-time VRAM peak. Frees only
                # unused cached blocks, never live tensors → no quality impact.
                gc.collect()
                torch.cuda.empty_cache()
    else:
        _LOGGER.info("Running on CPU "
                     "(RTF will be lower than GPU — see docs/PERFORMANCE.md)")

    return models


def _warmup(core: PolyglotCore) -> None:
    """Warm up CUDA kernels + worker threads with a short synthesis per loaded lang."""
    if not _bool_env("POCKET_TTS_WARMUP", True):
        _LOGGER.info("Warmup skipped (POCKET_TTS_WARMUP=false)")
        return
    default_state_per_lang = core.voice_states.get(core.default_voice)
    if not default_state_per_lang:
        _LOGGER.info("Warmup skipped — default voice not encoded")
        return

    warmup_texts = {
        "german_24l":      ["Moment.", "Das ist ein kurzer Aufwärmtest."],
        "french_24l":      ["Moment.", "Ceci est un test rapide."],
        "english_2026-04": ["Sure.",   "This is a short warmup test."],
        "italian_24l":     ["Certo.",  "Questo è un test rapido."],
        "spanish_24l":     ["Claro.",  "Esta es una prueba rápida."],
        "portuguese_24l":  ["Claro.",  "Este é um teste rápido."],
    }
    _LOGGER.info("Warming up %d model(s) ...", len(core.models))
    for ckpt, model in core.models.items():
        state = default_state_per_lang.get(ckpt)
        if state is None:
            continue
        texts = warmup_texts.get(ckpt, ["Hello.", "This is a warmup."])
        t0 = time.perf_counter()
        n_frames = 0
        try:
            for text in texts:
                for _f in model.generate_audio_stream(state, text):
                    n_frames += 1
            dt_ms = int((time.perf_counter() - t0) * 1000)
            _LOGGER.info("Warmup %s: %d ms, %d frames", ckpt, dt_ms, n_frames)
        except Exception as e:
            _LOGGER.warning("Warmup %s failed: %s", ckpt, e)
    # Return the warmup high-water reservation to the OS (PyTorch's caching
    # allocator holds it otherwise). Steady-state drops toward the real working
    # set; the compiled kernels and model weights stay — no quality impact.
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass
    _LOGGER.info("Warmup complete")


async def _run_endpoints(
    core: PolyglotCore,
    host: str,
    wyoming_port: int | None,
    http_port: int | None,
) -> None:
    tasks = []

    if wyoming_port is not None:
        from .wyoming_server import run_wyoming_server
        tasks.append(asyncio.create_task(
            run_wyoming_server(core, host, wyoming_port),
            name="wyoming",
        ))
    else:
        _LOGGER.info("Wyoming endpoint disabled (POCKET_TTS_WYOMING_PORT empty)")

    if http_port is not None:
        import uvicorn
        from .http_server import build_app
        from .ui_server import mount_ui
        app = build_app(core, core.voices_extra_dir)
        mount_ui(app, core)
        config = uvicorn.Config(
            app, host=host, port=http_port,
            log_level="info", access_log=False, lifespan="off",
        )
        server = uvicorn.Server(config)
        _LOGGER.info("HTTP endpoint (OpenAI-Speech) listening on %s:%d",
                     host, http_port)
        tasks.append(asyncio.create_task(server.serve(), name="http"))
    else:
        _LOGGER.info("HTTP endpoint disabled (POCKET_TTS_HTTP_PORT empty)")

    if not tasks:
        raise SystemExit("No endpoints enabled — set at least one of "
                         "POCKET_TTS_WYOMING_PORT or POCKET_TTS_HTTP_PORT")

    await asyncio.gather(*tasks)


async def run() -> None:
    logging.basicConfig(
        level=os.environ.get("POCKET_TTS_LOG_LEVEL", "INFO"),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    from . import __version__
    _LOGGER.info("Polyglot TTS v%s starting", __version__)

    # Overlay UI-saved settings (settings.json) onto the environment BEFORE
    # anything reads os.environ, so UI edits take effect on the next start.
    from . import config_store
    config_store.apply_overlay()

    # Install the user replacement dictionary (config/replacements.json) into
    # text_norm. Editable live via the UI/REST — this is just the boot load.
    from .text_norm import set_replacements
    set_replacements(config_store.read_replacements())

    # POCKET_TTS_LANGUAGES is the canonical key. POCKET_TTS_LANGUAGE (singular)
    # is the back-compat fallback for users migrating from araa47's fork —
    # see docs/MIGRATION_FROM_ARAA47.md.
    languages_env = os.environ.get(
        "POCKET_TTS_LANGUAGES",
        os.environ.get(
            "POCKET_TTS_LANGUAGE",
            "english_2026-04,german_24l,french_24l",
        ),
    )
    checkpoints = [s.strip() for s in languages_env.split(",") if s.strip()]
    default_voice = os.environ.get("POCKET_TTS_VOICE", "eve")
    voices_dir = Path(os.environ.get("POCKET_TTS_VOICES_DIR", "/app/voices"))
    voices_extra_dir = Path(os.environ.get(
        "POCKET_TTS_VOICES_EXTRA_DIR", "/app/voices-extra"))
    host = os.environ.get("POCKET_TTS_HOST", "0.0.0.0")
    device = os.environ.get("POCKET_TTS_DEVICE", "auto").lower()
    timing_port = _int_env("POCKET_TTS_TIMING_PORT", 10299)
    wyoming_port = _int_env("POCKET_TTS_WYOMING_PORT", 10200)
    http_port = _int_env("POCKET_TTS_HTTP_PORT", 10201)

    # HuggingFace token resolution (only needed for voice cloning — the gated
    # kyutai/pocket-tts model). Order: HF_TOKEN env wins; otherwise read it
    # from the file named by HF_TOKEN_FILE (Docker-secret friendly). We set
    # HF_TOKEN in the environment so huggingface_hub picks it up.
    # Token presence is deliberately NOT logged.
    if not os.environ.get("HF_TOKEN"):
        token_file = os.environ.get("HF_TOKEN_FILE")
        if token_file and os.path.isfile(token_file):
            try:
                with open(token_file, encoding="utf-8") as fh:
                    tok = fh.read().strip()
                if tok:
                    os.environ["HF_TOKEN"] = tok
            except OSError:
                pass

    # 1) Load models
    models = _load_models(checkpoints, device)

    # 2) Build core
    advertised_bcp47 = sorted({
        LANGUAGE_TO_BCP47.get(ckpt.split("_")[0], "en") for ckpt in models.keys()
    })
    core = PolyglotCore(
        models=models,
        voice_states={},
        default_voice=default_voice,
        advertised_bcp47=advertised_bcp47,
        voices_extra_dir=voices_extra_dir,
    )

    # 3) Encode default voice (if it's a preset)
    if default_voice and default_voice not in core.voice_states:
        _LOGGER.info("Encoding default voice: %s", default_voice)
        per_lang = core.encode_voice(default_voice)
        if per_lang:
            core.add_voice(default_voice, per_lang)
        else:
            _LOGGER.error("Default voice %r could not be encoded "
                          "against any loaded language", default_voice)

    # 4) Load initial voices from voices-extra (synchronous; later changes via watcher)
    load_initial_voices(core, [voices_extra_dir])

    # 5) Warmup
    _warmup(core)

    # 6) Start timing side-channel
    if timing_port is not None:
        start_timing_server(host=host, port=timing_port)

    # 7) Start file-watcher
    observer = start_watcher(core, voices_extra_dir)

    # 8) Run endpoints
    try:
        await _run_endpoints(core, host, wyoming_port, http_port)
    finally:
        observer.stop()
        observer.join(timeout=5)


def run_sync() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    run_sync()
