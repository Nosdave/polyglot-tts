# Changelog

All notable changes will be documented here. Semantic versioning.

## [0.5.2] — 2026-05-31

### Changed

- CI now builds `linux/arm64` variants of both `:latest` (CPU) and
  `:cuda` natively on GitHub-hosted `ubuntu-24.04-arm` runners. The
  `:cuda` tag is now a multi-arch manifest covering `linux/amd64` +
  `linux/arm64` — NVIDIA DGX Spark (Grace + GB10), Jetson Orin/AGX,
  and amd64 NVIDIA hosts all `docker pull ghcr.io/nosdave/polyglot-tts:cuda`
  to get the right image automatically.
- Workflow uses the digest-based multi-platform pattern (per-arch
  build → merge by manifest list) so each arch builds on its native
  runner — no QEMU, no self-hosted Spark dependency, no manual push.

### Docs

- `docs/SPARK_BUILD.md` recast as "local-build for special cases"
  rather than "Spark users must do this". Default path is the
  multi-arch `:cuda` image.
- README's GPU bullet and use-cases table updated to reflect Spark /
  Jetson as first-class targets covered by the published image.
- `docker-compose.example.yaml` adds an explicit comment confirming
  the `:cuda` profile works on Spark/Jetson.

### Why the cleanup matters

Through v0.5.1, ARM64+CUDA users were directed to a local-build flow.
GitHub-hosted ARM64 runners went GA for public repos in August 2025,
making that constraint unnecessary. Multi-arch `:cuda` distribution is
now standard for the public image lineup.

## [0.5.1] — 2026-05-31

Post-launch hardening pass driven by an independent multi-agent code +
security + concurrency + docs review.

### Fixed (security)

- `POST /v1/audio/voices` now streams uploads to disk with a 50 MB
  hard cap. Previously the entire payload was buffered in RAM, which
  could OOM-kill a 4 GB host (HA Green, Pi 5) with a single curl.
- `DELETE /v1/audio/voices/{name}` and `POST /v1/audio/voices` now
  reject names containing path separators, `..`, or leading dots.
  Previously path-traversal was possible against the voices-extra/
  directory.
- `POST /v1/audio/speech` now caps the `input` field at 4000 characters
  to prevent GPU/CPU saturation via a single oversized request.
- Synthesis error responses no longer echo internal exception detail
  to the caller; tracebacks remain in server logs only.
- Filesystem path resolution now verifies that resolved paths stay
  under `voices-extra/` before unlink (defence-in-depth against
  symlink-escape).

### Fixed (concurrency)

- Added a shared per-model `threading.Lock` on `PolyglotCore` so the
  HTTP synth path no longer races with the Wyoming path's mutation of
  pocket-tts's non-thread-safe per-model state.
- Voice-state mutations from the HTTP on-demand-encode path now go
  through `core.set_voice_state()` under the existing voice lock,
  replacing the unguarded `voice_states[…][…] = …` write.
- On-demand preset encoding wrapped in try/except — a pocket-tts
  exception on an unknown preset now returns HTTP 404 instead of 500.

### Fixed (build)

- `pyproject.toml`: dropped the redundant `License :: OSI Approved ::
  MIT License` classifier. Newer setuptools (PEP 639) rejects this
  when `license = "MIT"` is also set; was the root cause of the
  first GHA build failing.
- GitHub Actions workflow: dropped `flavor: suffix=-cuda` which
  combined with `type=raw,value=cuda` produced `:cuda-cuda` tags
  instead of the intended `:cuda`.

### Fixed (correctness)

- `dispatcher.py` now passes the configured `POCKET_TTS_HOST` to
  `start_timing_server`. Previously the timing endpoint hardcoded
  `0.0.0.0` regardless of the env-declared bind address.
- `POCKET_TTS_LANGUAGE` (singular) is now read as a back-compat
  fallback for users migrating from araa47's fork, as advertised in
  the migration guide.

### Hardened

- Container now runs as a non-root system user (UID 10001).
  `HF_HOME` and `HOME` redirected to `/app`. Compose-mounted volumes
  must be writable by UID 10001.
- `python-multipart` floor bumped to `>=0.0.18` (CVE GHSA-59g5-xgcq-4qw3).
- `uvicorn[standard]` floor bumped to `>=0.30.0` (clears websockets CVE
  in the `[standard]` extra).
- `HF_TOKEN` presence is no longer logged on startup (silent is safer
  for log-aggregator alerting).

### Docs

- NOTICE corrected from "28" to "26" pre-made voices.
- README's CUDA-13 wording clarified — the published `:cuda` image is
  CUDA 12.8 only; CUDA 13 / ARM64-GPU is build-from-source.
- CONFIGURATION.md: `POCKET_TTS_LAZY_LOAD` now explicitly flagged as
  declared-but-not-yet-implemented.
- HOME_ASSISTANT.md: removed references to the unimplemented
  `POCKET_TTS_MIN_LID_CHARS` env var, replaced with the correct
  20-char threshold and explicit-hint workaround.

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
