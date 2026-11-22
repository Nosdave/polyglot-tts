"""Management token: every state-changing HTTP route needs it, synthesis and
read-only routes do not. Also covers where the token comes from (env, generated
file), upload and settings validation, and the login parts of the web UI.

No model is loaded: the app runs against a PolyglotCore with a dummy model,
and synthesis is exercised through the chime marker (no inference).
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from polyglot_tts import auth, config_store
from polyglot_tts.core import CHIME_MARKER, PolyglotCore
from polyglot_tts.http_server import build_app
from polyglot_tts.ui_server import mount_ui

WEB_DIR = Path(auth.__file__).parent / "web"
ENV_TOKEN = "test-token-0123456789abcdef"


class _FakeModel:
    sample_rate = 24000
    temp = 0.3


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    """Isolated config dir + clean env; restores everything afterwards."""
    for key in (*config_store.EDITABLE_KEYS, auth.TOKEN_ENV):
        monkeypatch.delenv(key, raising=False)  # also restores on teardown
    d = tmp_path / "config"
    monkeypatch.setenv("POCKET_TTS_CONFIG_FILE", str(d / "settings.json"))
    auth._reset_for_tests()
    yield d
    auth._reset_for_tests()


@pytest.fixture
def voices_dir(tmp_path):
    d = tmp_path / "voices-extra"
    d.mkdir()
    return d


@pytest.fixture
def core(voices_dir):
    return PolyglotCore(
        models={"german_24l": _FakeModel()},
        voice_states={},
        default_voice="eve",
        advertised_bcp47=["de"],
        voices_extra_dir=voices_dir,
    )


@pytest.fixture
def app(cfg_dir, core, voices_dir, monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV, ENV_TOKEN)
    application = build_app(core, voices_dir)
    mount_ui(application, core)
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def _bearer(token: str = ENV_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _wav_upload(filename: str = "sample.wav", data: bytes = b"RIFF0000WAVE"):
    return {"file": (filename, data, "audio/wav")}


@pytest.fixture
def no_restart(monkeypatch):
    """The restart route re-execs the process from a thread — never in tests."""
    started = []

    class _Thread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            started.append(True)

    monkeypatch.setattr("polyglot_tts.ui_server.threading.Thread", _Thread)
    return started


# ── Where the token comes from ──────────────────────────────────────────────


def test_token_is_generated_persisted_and_private(cfg_dir, caplog):
    caplog.set_level(logging.DEBUG)
    tok = auth.get_token()
    auth.announce()
    path = cfg_dir / "ui_token"
    assert path == auth.token_path()
    assert path.read_text().strip() == tok
    assert len(tok) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth.token_source() == "file"
    # no temp file left behind
    assert [p.name for p in cfg_dir.iterdir()] == ["ui_token"]
    # the log names the file, never the value
    assert str(path) in caplog.text
    assert tok not in caplog.text


def test_token_survives_restart_and_is_announced_once(cfg_dir, caplog):
    first = auth.get_token()
    auth._reset_for_tests()  # "restart": module state gone, file stays
    caplog.set_level(logging.INFO)
    auth.announce()
    auth.announce()
    assert auth.get_token() == first
    assert caplog.text.count("Management token:") == 1


def test_env_token_takes_precedence_and_writes_no_file(cfg_dir, monkeypatch, caplog):
    monkeypatch.setenv(auth.TOKEN_ENV, ENV_TOKEN)
    caplog.set_level(logging.DEBUG)
    auth.announce()
    assert auth.get_token() == ENV_TOKEN
    assert auth.token_source() == "env"
    assert not (cfg_dir / "ui_token").exists()
    assert ENV_TOKEN not in caplog.text


def test_env_token_wins_over_existing_file(cfg_dir, monkeypatch):
    generated = auth.get_token()
    monkeypatch.setenv(auth.TOKEN_ENV, ENV_TOKEN)
    assert auth.get_token() == ENV_TOKEN
    assert auth.token_matches(generated) is False


def test_short_env_token_warns(cfg_dir, monkeypatch, caplog):
    monkeypatch.setenv(auth.TOKEN_ENV, "short")
    caplog.set_level(logging.WARNING)
    auth.announce()
    assert "shorter than" in caplog.text
    assert auth.token_matches("short")  # still honoured: operator's choice


def test_unusable_token_file_is_replaced(cfg_dir):
    cfg_dir.mkdir(parents=True)
    path = cfg_dir / "ui_token"
    for junk in ("", "abc", "has white space in it!!", "x" * 5000):
        path.write_text(junk)
        auth._reset_for_tests()
        tok = auth.get_token()
        assert tok != junk.strip() and len(tok) >= 32
        assert path.read_text().strip() == tok
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_wide_permissions_on_existing_file_are_tightened(cfg_dir):
    cfg_dir.mkdir(parents=True)
    path = cfg_dir / "ui_token"
    path.write_text("operator-chosen-token-123456\n")
    os.chmod(path, 0o644)
    assert auth.get_token() == "operator-chosen-token-123456"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unwritable_config_dir_falls_back_to_a_run_token(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")  # parent of the config "dir" is a file
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    monkeypatch.setenv("POCKET_TTS_CONFIG_FILE", str(blocker / "cfg" / "settings.json"))
    auth._reset_for_tests()
    caplog.set_level(logging.INFO)
    try:
        tok = auth.get_token()
        auth.announce()
        auth.announce()
        assert auth.token_source() == "memory"
        assert auth.get_token() == tok  # stable for this run
        assert caplog.text.count(tok) == 1  # shown exactly once
    finally:
        auth._reset_for_tests()


def test_comparison_is_constant_time(cfg_dir, monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV, ENV_TOKEN)
    calls = []
    real = auth.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    assert auth.token_matches(ENV_TOKEN) is True
    assert auth.token_matches(ENV_TOKEN + "x") is False
    assert auth.token_matches("tökén-with-non-ascii") is False
    assert len(calls) == 3
    assert auth.token_matches("") is False and auth.token_matches(None) is False


# ── Mutating routes: 401 without the token, served with it ──────────────────

MUTATING = [
    ("POST", "/v1/audio/voices"),
    ("DELETE", "/v1/audio/voices/somevoice"),
    ("POST", "/api/ui/config"),
    ("POST", "/api/ui/replacements"),
    ("POST", "/api/ui/restart"),
]


@pytest.mark.parametrize(("method", "path"), MUTATING)
def test_mutating_route_rejects_missing_and_wrong_token(client, no_restart, method, path):
    for headers in (
        {},
        _bearer("wrong-token"),
        {"Authorization": f"Basic {ENV_TOKEN}"},
        {"Authorization": ENV_TOKEN},
        {"X-UI-Token": "wrong"},
    ):
        r = client.request(method, path, headers=headers)
        assert r.status_code == 401, (headers, r.text)
        assert r.headers["www-authenticate"] == "Bearer"
        assert ENV_TOKEN not in r.text
    assert client.request(method, path, params={"token": "wrong"}).status_code == 401
    assert no_restart == []


def test_mutating_routes_work_with_token(client, core, voices_dir, cfg_dir, no_restart):
    h = _bearer()
    r = client.post("/v1/audio/voices", headers=h, files=_wav_upload(), data={"name": "myvoice"})
    assert r.status_code == 201, r.text
    assert (voices_dir / "myvoice.wav").is_file()

    assert client.delete("/v1/audio/voices/myvoice", headers=h).status_code == 204
    assert not (voices_dir / "myvoice.wav").exists()

    r = client.post(
        "/api/ui/config", headers=h, json={"updates": {"POCKET_TTS_MIN_LID_CHARS": "12"}}
    )
    assert r.status_code == 200 and r.json()["saved"] is True

    r = client.post(
        "/api/ui/replacements", headers=h, json={"replacements": {"HA": "Home Assistant"}}
    )
    assert r.status_code == 200 and r.json()["count"] == 1

    r = client.post("/api/ui/restart", headers=h)
    assert r.status_code == 200 and no_restart == [True]


def test_every_registered_unsafe_route_needs_the_token(app, client, no_restart):
    """Walk the route table: whatever is not GET/HEAD/OPTIONS and not on the
    open list must answer 401 without a token — also for routes added later."""
    checked = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path.replace("{name}", "x")
        for method in route.methods - auth.SAFE_METHODS:
            if (method, route.path) in auth.OPEN_UNSAFE_ROUTES:
                continue
            assert client.request(method, path).status_code == 401, (method, path)
            checked.add((method, route.path))
    assert {
        ("POST", "/v1/audio/voices"),
        ("DELETE", "/v1/audio/voices/{name}"),
        ("POST", "/api/ui/config"),
        ("POST", "/api/ui/replacements"),
        ("POST", "/api/ui/restart"),
    } <= checked
    assert no_restart == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/ui/restart/"),  # trailing slash
        ("POST", "//api/ui/restart"),  # doubled slash
        ("POST", "/api/ui/config/"),
        ("POST", "/v1/audio/voices/"),
        ("DELETE", "/v1/audio/voices/x/"),
        ("PUT", "/api/ui/config"),  # other verbs
        ("PATCH", "/v1/audio/voices/x"),
        ("DELETE", "/api/ui/replacements"),
        ("POST", "/ui/static/app.js"),  # mounted sub-app
        ("POST", "/no/such/route"),
        ("POST", "/v1/audio/speech/extra"),  # only the exact open paths are open
        ("DELETE", "/v1/audio/speech"),
    ],
)
def test_default_deny_for_unsafe_methods(client, no_restart, method, path):
    assert client.request(method, path).status_code == 401
    assert no_restart == []


def test_safe_methods_never_reach_a_mutating_handler(client, voices_dir, no_restart):
    (voices_dir / "keep.wav").write_bytes(b"x")
    for method in ("GET", "HEAD", "OPTIONS"):
        for path in ("/api/ui/restart", "/v1/audio/voices/keep", "/api/ui/restart/"):
            r = client.request(method, path)
            assert r.status_code in (404, 405), (method, path, r.status_code)
    assert no_restart == []
    assert (voices_dir / "keep.wav").exists()


def test_rejection_happens_before_the_body_is_read(app):
    """A client without a token must not be able to make the server spool an
    upload: the 401 is sent without consuming the request body."""
    sent = []

    async def receive():
        raise AssertionError("request body was read before the auth check")

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/audio/voices",
        "raw_path": b"/v1/audio/voices",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"multipart/form-data; boundary=x"),
            (b"content-length", b"52428800"),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 401


def test_accepted_ways_to_send_the_token(client):
    url = "/api/ui/session"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=_bearer()).status_code == 200
    assert client.get(url, headers={"Authorization": f"bearer {ENV_TOKEN}"}).status_code == 200
    # legacy mechanisms of the old optional UI token keep working
    assert client.get(url, headers={"X-UI-Token": ENV_TOKEN}).status_code == 200
    assert client.get(url, params={"token": ENV_TOKEN}).status_code == 200
    assert client.get(url, headers=_bearer(ENV_TOKEN[:-1])).status_code == 401
    assert client.get(url, headers=_bearer()).json() == {"authenticated": True}


def test_generated_token_protects_when_no_env_token_is_set(cfg_dir, core, voices_dir, no_restart):
    """Secure by default: nothing configured → a token exists and is enforced."""
    application = build_app(core, voices_dir)
    mount_ui(application, core)
    c = TestClient(application)
    tok = (cfg_dir / "ui_token").read_text().strip()
    assert c.post("/api/ui/restart").status_code == 401
    assert c.delete("/v1/audio/voices/x").status_code == 401
    assert c.get("/api/ui/session", headers=_bearer(tok)).status_code == 200
    assert c.get("/api/ui/status").json()["token_source"] == "file"
    assert no_restart == []


# ── Synthesis and read-only routes stay open ────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/health",
        "/v1/audio/voices",
        "/v1/audio/languages",
        "/ui",
        "/ui/static/app.js",
        "/ui/static/style.css",
        "/api/ui/status",
        "/api/ui/config",
        "/api/ui/replacements",
    ],
)
def test_read_routes_need_no_token(client, path):
    assert client.get(path).status_code == 200


def test_synthesis_needs_no_token(client):
    r = client.post("/v1/audio/speech", json={"input": CHIME_MARKER, "response_format": "wav"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"
    # a client that used the trailing-slash form still gets the router redirect
    r = client.post(
        "/v1/audio/speech/",
        follow_redirects=False,
        json={"input": CHIME_MARKER, "response_format": "wav"},
    )
    assert r.status_code == 307


def test_normalize_preview_needs_no_token(client):
    r = client.post("/v1/text/normalize", json={"input": "Hallo", "language": "de"})
    assert r.status_code == 200 and "normalized" in r.json()


# ── The token never shows up in a response ──────────────────────────────────


def test_token_is_not_exposed_by_any_open_route(client):
    for path in (
        "/",
        "/health",
        "/ui",
        "/ui/static/app.js",
        "/ui/static/index.html",
        "/api/ui/status",
        "/api/ui/config",
        "/api/ui/replacements",
        "/v1/audio/voices",
        "/openapi.json",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert ENV_TOKEN not in r.text, path
    assert auth.TOKEN_ENV not in client.get("/api/ui/config").json()["config"]
    status = client.get("/api/ui/status").json()
    assert status["auth_enabled"] is True and status["token_source"] == "env"


def test_token_cannot_be_set_through_settings(client, cfg_dir):
    assert auth.TOKEN_ENV not in config_store.EDITABLE_KEYS
    r = client.post(
        "/api/ui/config",
        headers=_bearer(),
        json={"updates": {auth.TOKEN_ENV: "attacker-chosen-token-1234"}},
    )
    assert r.status_code == 200
    assert auth.get_token() == ENV_TOKEN
    assert auth.TOKEN_ENV not in config_store._read_file()


# ── Upload hardening ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename",
    [
        "shell.php",
        "run.sh",
        "page.html",
        "voice.wav.error",
        "cfg.yaml",
        "state.safetensors",
        "recording.webm",
        "noext",
        "archive.tar.gz",
    ],
)
def test_upload_rejects_non_audio_suffix(client, voices_dir, filename):
    r = client.post(
        "/v1/audio/voices", headers=_bearer(), files=_wav_upload(filename), data={"name": "v1"}
    )
    # "noext" has no suffix and is stored as .wav, as before
    if filename == "noext":
        assert r.status_code == 201
        assert [p.name for p in voices_dir.iterdir()] == ["v1.wav"]
    else:
        assert r.status_code == 400, r.text
        assert list(voices_dir.iterdir()) == []


@pytest.mark.parametrize(
    "suffix", [".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".WAV"]
)
def test_upload_accepts_audio_suffixes(client, voices_dir, suffix):
    r = client.post(
        "/v1/audio/voices",
        headers=_bearer(),
        files=_wav_upload("in" + suffix),
        data={"name": "v1", "language": "de"},
    )
    assert r.status_code == 201, r.text
    assert (voices_dir / f"v1.de{suffix.lower()}").is_file()


def test_upload_keeps_name_validation_and_size_cap(client, voices_dir, monkeypatch):
    for bad in ("../evil", "..", ".hidden", "a/b", "a\\b", "x y"):
        r = client.post(
            "/v1/audio/voices", headers=_bearer(), files=_wav_upload(), data={"name": bad}
        )
        assert r.status_code == 400, bad
    monkeypatch.setattr("polyglot_tts.http_server.MAX_VOICE_UPLOAD_BYTES", 10)
    r = client.post(
        "/v1/audio/voices",
        headers=_bearer(),
        files=_wav_upload(data=b"x" * 64),
        data={"name": "big"},
    )
    assert r.status_code == 413
    assert list(voices_dir.iterdir()) == []


def test_upload_caps_the_number_of_voices(client, voices_dir, monkeypatch):
    monkeypatch.setattr("polyglot_tts.http_server.MAX_EXTRA_VOICES", 2)
    for name in ("one", "two"):
        assert (
            client.post(
                "/v1/audio/voices", headers=_bearer(), files=_wav_upload(), data={"name": name}
            ).status_code
            == 201
        )
    r = client.post(
        "/v1/audio/voices", headers=_bearer(), files=_wav_upload(), data={"name": "three"}
    )
    assert r.status_code == 409
    assert not (voices_dir / "three.wav").exists()
    # another language file for an existing voice is not a new voice
    r = client.post(
        "/v1/audio/voices",
        headers=_bearer(),
        files=_wav_upload(),
        data={"name": "one", "language": "de"},
    )
    assert r.status_code == 201


def test_unauthenticated_upload_writes_nothing(client, voices_dir):
    r = client.post("/v1/audio/voices", files=_wav_upload(), data={"name": "v1"})
    assert r.status_code == 401
    assert list(voices_dir.iterdir()) == []


# ── Settings: languages and default voice are names, never locations ────────

BAD_LANGUAGES = [
    "../../../../tmp/evil",
    "/tmp/evil",
    "..",
    ".",
    "german_24l/../../x",
    "german_24l.yaml",
    "hf://someone/repo/cfg",
    "https://example.invalid/cfg",
    "german_24l,../x",
    "klingon_24l",
    "german 24l",
    "-german",
    "a" * 80,
    "C:\\evil",
    "german_24l\x00",
]


@pytest.mark.parametrize("value", BAD_LANGUAGES)
def test_config_rejects_bad_language_values(client, cfg_dir, value):
    r = client.post(
        "/api/ui/config", headers=_bearer(), json={"updates": {"POCKET_TTS_LANGUAGES": value}}
    )
    assert r.status_code == 400, r.text
    assert "POCKET_TTS_LANGUAGES" not in os.environ
    assert not (cfg_dir / "settings.json").exists()


def test_config_accepts_known_languages(client, cfg_dir):
    r = client.post(
        "/api/ui/config",
        headers=_bearer(),
        json={"updates": {"POCKET_TTS_LANGUAGES": " german_24l , french_24l,german "}},
    )
    assert r.status_code == 200, r.text
    assert config_store._read_file()["POCKET_TTS_LANGUAGES"] == "german_24l,french_24l"
    assert r.json()["restart_required_for"] == ["POCKET_TTS_LANGUAGES"]


BAD_VOICES = [
    "http://127.0.0.1:9/voice.wav",
    "https://example.invalid/v.safetensors",
    "hf://someone/repo/voice.wav",
    "/etc/hostname",
    "../voices/x.wav",
    "file:///etc/hostname",
    "no_such_voice",
    "eve.safetensors",
    "eve\n",
]


@pytest.mark.parametrize("value", BAD_VOICES)
def test_config_rejects_unknown_default_voice(client, core, cfg_dir, value):
    r = client.post(
        "/api/ui/config", headers=_bearer(), json={"updates": {"POCKET_TTS_VOICE": value}}
    )
    assert r.status_code == 400, r.text
    assert core.default_voice == "eve"
    assert "POCKET_TTS_VOICE" not in os.environ
    assert not (cfg_dir / "settings.json").exists()


def test_config_accepts_builtin_and_loaded_voices(client, core):
    r = client.post(
        "/api/ui/config", headers=_bearer(), json={"updates": {"POCKET_TTS_VOICE": "alba"}}
    )
    assert r.status_code == 200 and core.default_voice == "alba"
    core.add_voice("My Cloned.Voice", {"german_24l": object()})
    r = client.post(
        "/api/ui/config",
        headers=_bearer(),
        json={"updates": {"POCKET_TTS_VOICE": "My Cloned.Voice"}},
    )
    assert r.status_code == 200 and core.default_voice == "My Cloned.Voice"


@pytest.mark.parametrize(
    "updates",
    [
        {"POCKET_TTS_DEVICE": "x" * 5000},
        {"POCKET_TTS_DEVICE": "cu\x00da"},
        {"HF_TOKEN": "hf_abc\r\nX-Injected: 1"},
        {"POCKET_TTS_TEMP": {"nested": "object"}},
        {"POCKET_TTS_TEMP": ["list"]},
    ],
)
def test_config_rejects_malformed_values(client, cfg_dir, updates):
    r = client.post("/api/ui/config", headers=_bearer(), json={"updates": updates})
    assert r.status_code == 400, r.text
    assert not (cfg_dir / "settings.json").exists()


def test_config_rejects_malformed_bodies(client):
    h = _bearer()
    assert client.post("/api/ui/config", headers=h, json=["not", "an", "object"]).status_code == 400
    assert client.post("/api/ui/config", headers=h, content=b"{not json").status_code == 400
    assert client.post("/api/ui/replacements", headers=h, json=[1, 2]).status_code == 400


# ── The same rules at startup (settings file or environment) ────────────────


def test_startup_drops_bad_language_entries(cfg_dir, caplog):
    from polyglot_tts import dispatcher

    caplog.set_level(logging.ERROR)
    got = dispatcher._resolve_checkpoints("../../tmp/evil, german_24l ,hf://x/y/z,french_24l")
    assert got == ["german_24l", "french_24l"]
    assert os.environ["POCKET_TTS_LANGUAGES"] == "german_24l,french_24l"
    assert "Ignoring POCKET_TTS_LANGUAGES entry" in caplog.text


def test_startup_falls_back_to_defaults_instead_of_failing(cfg_dir, caplog):
    from polyglot_tts import dispatcher

    caplog.set_level(logging.ERROR)
    for bad in ("/tmp/evil", "../x,..", "", " , "):
        assert dispatcher._resolve_checkpoints(bad) == dispatcher.DEFAULT_LANGUAGES.split(",")
    assert "loading the defaults" in caplog.text


def test_startup_keeps_a_valid_language_setting_untouched(cfg_dir):
    from polyglot_tts import dispatcher

    assert dispatcher._resolve_checkpoints("english_2026-10,german") == [
        "english_2026-10",
        "german",
    ]
    assert "POCKET_TTS_LANGUAGES" not in os.environ


def test_startup_default_voice_must_be_a_known_name(cfg_dir, caplog):
    from polyglot_tts import dispatcher

    caplog.set_level(logging.ERROR)
    loaded = {"EL_Custom": {}}
    assert dispatcher._resolve_default_voice("EL_Custom", loaded) == "EL_Custom"
    assert dispatcher._resolve_default_voice("alba", loaded) == "alba"
    assert "POCKET_TTS_VOICE" not in os.environ and caplog.text == ""
    for bad in BAD_VOICES + [""]:
        assert dispatcher._resolve_default_voice(bad, loaded) == "eve", bad
    assert os.environ["POCKET_TTS_VOICE"] == "eve"
    assert "names only" in caplog.text


def test_tampered_settings_file_cannot_break_or_redirect_startup(cfg_dir, caplog):
    """A settings file written by an older version (or by hand) goes through
    the same checks when it is read back."""
    import json

    from polyglot_tts import dispatcher

    cfg_dir.mkdir(parents=True)
    (cfg_dir / "settings.json").write_text(
        json.dumps(
            {
                "POCKET_TTS_LANGUAGES": "../../../tmp/evil",
                "POCKET_TTS_VOICE": "https://example.invalid/voice.wav",
                "POCKET_TTS_DEVICE": "cu\u0000da",  # cannot go into os.environ
                "POCKET_TTS_UI_TOKEN": "attacker-chosen-token-1234",
                "PATH": "/tmp/evil",
            }
        )
    )
    path_before = os.environ.get("PATH")
    caplog.set_level(logging.ERROR)
    config_store.apply_overlay()  # must not raise
    assert os.environ.get("PATH") == path_before
    assert auth.TOKEN_ENV not in os.environ
    assert "POCKET_TTS_DEVICE" not in os.environ
    checkpoints = dispatcher._resolve_checkpoints(os.environ["POCKET_TTS_LANGUAGES"])
    assert checkpoints == dispatcher.DEFAULT_LANGUAGES.split(",")
    assert dispatcher._resolve_default_voice(os.environ["POCKET_TTS_VOICE"], {}) == "eve"


def test_filter_checkpoints_only_passes_bare_known_names():
    ok, bad = config_store.filter_checkpoints(
        ["german_24l", " english_2026-10 ", "", "../german_24l", "german_24l/", "nope"]
    )
    assert ok == ["german_24l", "english_2026-10"]
    assert bad == ["../german_24l", "german_24l/", "nope"]


# ── Web UI: login flow present, page hardened ───────────────────────────────


def test_ui_page_is_open_and_carries_protective_headers(client):
    r = client.get("/ui")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert (
        "script-src 'self'" in csp
        and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    )
    assert "frame-ancestors 'none'" in csp
    assert r.headers["referrer-policy"] == "no-referrer"
    # the static copy of the page gets the same policy
    assert "content-security-policy" in client.get("/ui/static/index.html").headers


def test_ui_assets_contain_the_login_flow():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    for element_id in (
        "login-btn",
        "logout-btn",
        "token-gate",
        "token-input",
        "token-save",
        "auth-state",
    ):
        assert f'id="{element_id}"' in html, element_id
    assert "<script>" not in html and "onclick=" not in html  # CSP: no inline script
    assert '"Bearer " + ' in js and "Authorization" in js
    assert "localStorage" in js and "polyglot_ui_token" in js
    assert "/api/ui/session" in js
    assert "requireLogin()" in js and "setLoggedOut" in js
    assert "res.status === 401" in js
    # the page reads a legacy ?token= once but never builds such a URL
    assert "token=" not in js.replace("?token=…", "")
    assert "X-UI-Token" not in js


def test_ui_escapes_server_values_before_inserting_markup():
    """Voice names and last-synthesis fields come from any client on the
    network; the page that holds the token must insert them as text."""
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "function esc(" in js
    for expr in (
        "esc(t.voice)",
        "esc(t.language)",
        "esc(s.default_voice)",
        "esc(v.name)",
        "esc(j.detail || r.status)",
    ):
        assert expr in js, expr
    for raw in ("${t.voice}", "${v.name}", "${s.default_voice}", "${s.device}"):
        assert raw not in js, raw
