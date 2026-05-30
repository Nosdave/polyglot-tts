# Changelog

All notable changes will be documented here. Semantic versioning.

## [0.5.0] — 2026-05-30

Initial public release. Fork of araa47/wyoming_pocket_tts with
substantial additions.

### Added

- OpenAI-Speech-compatible HTTP endpoint on TCP `:10201`
  (`POST /v1/audio/speech`, `GET /v1/audio/voices`,
  `GET /v1/audio/languages`, `GET /health`).
- Voice-management REST API (POST/DELETE on `/v1/audio/voices`).
- File-watcher on `voices-extra/` for drop-and-clone workflow.
  Auto-embeds new WAVs within 1–2 s, no restart needed.
- Multi-language same-voice loading: one cloned voice speaks every
  loaded language.
- Lingua-based per-sentence language detection (replaces py3langid).
- Mukser-Fix: 120-sample fade-in + tail padding to suppress
  ConvTranspose click on streaming chunk boundaries.
- num2words-driven text normalization (de / en / fr).
- HA-aware streaming text-IN handler with sentence-buffering and
  drain-loop architecture.
- Side-channel timing HTTP endpoint on `:10299` for observability.
- Multi-arch Docker image: `:latest` (linux/amd64 + linux/arm64 CPU)
  and `:cuda` (linux/amd64 CUDA 12.8).
- Environment-driven configuration — no YAML.
- Three example compose profiles: cpu, cuda, minimal.

### Attribution

Built upon:
- [Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts) — MIT (code), CC-BY 4.0 (models).
- [araa47/wyoming_pocket_tts](https://github.com/araa47/wyoming_pocket_tts) — MIT.

See [NOTICE](NOTICE) for the full attribution block.

### Known limitations

- Lazy-loading of additional languages (`POCKET_TTS_LAZY_LOAD=true`)
  is declared in the env-schema but not yet implemented. Will land in
  0.6.0.
- ARM64+CUDA13 builds for DGX Spark / Grace+Blackwell are
  production-verified locally but not yet in CI. Spark users build
  with `docker build --build-arg PYTORCH_INDEX=<spark-index>` for now.
