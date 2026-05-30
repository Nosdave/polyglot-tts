"""Tiny HTTP server exposing last-synthesis timing as JSON.

Workaround for HA-Pipeline-Streaming-aware-event-semantic where `tts-end` fires
on stream-URL-ready (~1ms) instead of actual synthesis-complete. External
dashboards (e.g. sparkdash) can poll this endpoint to get the authoritative
TTS-synth-time.

Endpoint: GET http://<host>:<port>/timing
Response (JSON):
    {
        "audio_ms":  9920,       // audio playback duration in ms
        "synth_ms":  1771,       // wall-clock time for model.generate_audio call
        "ttfa_ms":   325,        // wall-clock from Synthesize-receipt to first AudioChunk
        "voice":     "eve",      // voice used for last synthesis
        "language":  "de",       // BCP47 language used for last synthesis
        "text_len":  150,        // length of synthesized text (chars)
        "ts":        1779100123  // unix timestamp of last update
    }

If no synthesis has happened yet, all fields are 0/empty.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Module-level state. Updated atomically (dict replace, not field update) so
# concurrent reads always see a consistent snapshot.
_TIMING_LOCK = threading.Lock()
_LAST_TIMING: dict[str, Any] = {
    "audio_ms": 0,
    "synth_ms": 0,
    "ttfa_ms": 0,
    "voice": "",
    "language": "",
    "text_len": 0,
    "ts": 0,
}


def update_timing(
    audio_ms: int,
    synth_ms: int,
    ttfa_ms: int,
    voice: str,
    language: str,
    text_len: int,
) -> None:
    """Atomic replace of _LAST_TIMING. Call after each completed synthesis."""
    snapshot = {
        "audio_ms": int(audio_ms),
        "synth_ms": int(synth_ms),
        "ttfa_ms": int(ttfa_ms),
        "voice": str(voice)[:64],
        "language": str(language)[:8],
        "text_len": int(text_len),
        "ts": int(time.time()),
    }
    with _TIMING_LOCK:
        _LAST_TIMING.update(snapshot)


def get_timing() -> dict[str, Any]:
    with _TIMING_LOCK:
        return dict(_LAST_TIMING)


class _TimingHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/timing":
            body = json.dumps(get_timing()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"not found")

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        return


def start_timing_server(host: str = "0.0.0.0", port: int = 10299) -> ThreadingHTTPServer:
    """Start the timing HTTP-server in a daemon thread. Returns the server instance."""
    server = ThreadingHTTPServer((host, port), _TimingHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="timing-http")
    t.start()
    _LOGGER.info("Timing HTTP-Endpoint listening on http://%s:%d/timing", host, port)
    return server
