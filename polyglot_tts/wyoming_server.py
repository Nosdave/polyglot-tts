"""Wyoming-protocol endpoint wrapper.

Spins up a Wyoming AsyncServer that hands every incoming connection to a
fresh PocketTTSEventHandler bound to the shared PolyglotCore.

Used by the dispatcher when POCKET_TTS_WYOMING_PORT is set.
"""

from __future__ import annotations

import argparse
import logging
from functools import partial

from wyoming.server import AsyncServer

from .core import LANGUAGE_TO_BCP47, PolyglotCore
from .wyoming_handler import (
    ALL_PRESET_VOICES,
    PocketTTSEventHandler,
    get_wyoming_info,
)

_LOGGER = logging.getLogger(__name__)


def _build_cli_args(core: PolyglotCore, default_voice: str) -> argparse.Namespace:
    """The handler reads attributes off a cli_args namespace; build one."""
    ns = argparse.Namespace()
    ns.voice = default_voice
    ns.no_streaming = False
    ns.debug = False
    return ns


async def run_wyoming_server(
    core: PolyglotCore,
    host: str,
    port: int,
) -> None:
    """Start Wyoming server bound to PolyglotCore. Blocks until cancelled."""
    advertised_voices = sorted(
        set(ALL_PRESET_VOICES) | set(core.voice_states.keys())
    )
    advertised_bcp47 = sorted({
        LANGUAGE_TO_BCP47.get(ckpt.split("_")[0], "en")
        for ckpt in core.models.keys()
    })
    wyoming_info = get_wyoming_info(advertised_voices, advertised_bcp47)
    cli_args = _build_cli_args(core, core.default_voice)

    server = AsyncServer.from_uri(f"tcp://{host}:{port}")
    _LOGGER.info("Wyoming endpoint listening on %s:%d "
                 "(%d voices advertised, langs=%s)",
                 host, port, len(advertised_voices), advertised_bcp47)

    await server.run(
        partial(
            PocketTTSEventHandler,
            wyoming_info,
            cli_args,
            core.models,
            core.voice_states,
            advertised_bcp47,
            core,
        )
    )
