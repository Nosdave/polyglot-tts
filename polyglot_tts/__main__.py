"""Polyglot TTS entrypoint.

Run via:
    python -m polyglot_tts
    polyglot-tts                  (after pip install)

All configuration is via environment variables — see polyglot_tts.dispatcher
or docs/CONFIGURATION.md.
"""

from .dispatcher import run_sync as run


if __name__ == "__main__":
    run()
