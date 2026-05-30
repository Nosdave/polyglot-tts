# Contributing

This is a personal project, maintained on a best-effort basis. PRs and
issues are welcome, with these realities in mind:

- Response times vary. If something is broken urgently and you need a
  guaranteed-quick turnaround, fork.
- Architecture changes that significantly grow the maintenance surface
  may be declined, even if the code is good. Smaller, focused PRs are
  much more likely to land.
- This project will not migrate off the `claude` CLI / OpenAI-Speech /
  Wyoming standards toward a different protocol stack. If you want a
  different stack, that's a fork.

## Quick development setup

```bash
git clone https://github.com/Nosdave/polyglot-tts.git
cd polyglot-tts
python -m venv .venv
. .venv/bin/activate     # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pre-commit install
```

## Before submitting a PR

1. Run `ruff check . && ruff format --check .`
2. Run the test suite: `pytest`
3. If you added/changed a public-facing config, update `docs/CONFIGURATION.md`.
4. If you changed the OpenAI-Speech endpoint or Wyoming behaviour,
   update the integration docs.

## Scope guidance

In scope:
- Bug fixes, performance improvements, doc improvements.
- Additional language support (when Kyutai ships new checkpoints).
- Additional output formats on the HTTP endpoint.
- Integration guides for other consumer apps.

Out of scope:
- Different protocol layers (e.g. gRPC, custom WebSocket protocol that
  isn't OpenAI-Speech-compatible).
- Cloud-TTS bridging (Polyglot is local-first).
- GUI/Web admin interface. (May happen later, but not via PR right now.)

## Reporting bugs

Use [GitHub Issues](https://github.com/Nosdave/polyglot-tts/issues).
Please include:
- Polyglot version (visible in `/health`).
- Image tag (`:latest`, `:cuda`, etc.).
- Hardware / OS / Docker version.
- Minimal repro: `curl` command or compose file.
- `POCKET_TTS_LOG_LEVEL=DEBUG` log excerpt if applicable.
