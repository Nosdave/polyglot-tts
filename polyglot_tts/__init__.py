"""Polyglot TTS — multi-language streaming TTS server with voice cloning."""

# Single source of truth is pyproject.toml. When installed (the image), read
# the real version from package metadata; fall back to a literal in a bare
# source checkout. Keep the fallback in sync with pyproject on release.
try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    try:
        __version__ = _pkg_version("polyglot-tts")
    except PackageNotFoundError:
        __version__ = "0.7.1"
except Exception:  # noqa: BLE001
    __version__ = "0.7.1"
