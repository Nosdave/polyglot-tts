# Choosing a CUDA image: `:cuda` vs `:cuda13`

Polyglot TTS publishes two GPU image variants. Pick based on your GPU and
your NVIDIA driver version.

## Quick decision

| Your situation | Use |
|---|---|
| Any NVIDIA GPU, you're not sure | **`:cuda`** (safe default) |
| Older driver (< 580), Proxmox/ESXi passthrough with a pinned driver, enterprise/LTS host | **`:cuda`** |
| Pascal (GTX 10xx), Volta (V100), Maxwell (GTX 9xx) | **`:cuda`** — `:cuda13` won't run |
| RTX 50xx (sm_120) or NVIDIA DGX Spark / GB10 (sm_121), driver ≥ 580, want maximum speed | **`:cuda13`** |
| No NVIDIA GPU at all | **`:latest`** (CPU) |

```bash
docker pull ghcr.io/nosdave/polyglot-tts:cuda      # broad compatibility
docker pull ghcr.io/nosdave/polyglot-tts:cuda13    # newest GPUs, native sm_121
docker pull ghcr.io/nosdave/polyglot-tts:latest    # CPU only
```

All three are multi-arch (`linux/amd64` + `linux/arm64`).

## The details

### `:cuda` — CUDA 12.8 (cu128)

- **Driver requirement:** NVIDIA ≥ 525.
- **GPU support:** Turing (sm_75) through Blackwell.
- **On Blackwell GB10 / sm_121 (DGX Spark):** runs, but cu128's nvrtc
  does not know sm_121 natively, so kernels fall back to runtime-JIT /
  Triton paths. Measured ~5× real-time on a GB10 — fine for streaming
  voice (faster than real-time) but below native Blackwell speed.
- **Why it's the default:** broadest compatibility. Doesn't break anyone
  on older drivers or older GPUs.

### `:cuda13` — CUDA 13 (cu130)

- **Driver requirement:** NVIDIA ≥ **580.65.06**. Below that it will not
  start.
- **GPU support:** Turing (sm_75) through Blackwell. **Maxwell, Pascal,
  and Volta were dropped in CUDA 13** — a GTX 1080 / Tesla V100 cannot
  run this image.
- **On Blackwell sm_120/sm_121:** native kernels, no JIT fallback. This
  is the image to use for full DGX Spark / RTX 50xx performance.
- **Tracking:** see [issue #1](https://github.com/Nosdave/polyglot-tts/issues/1)
  for the RTF comparison work.

## How to check your driver and GPU

```bash
nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv
```

- `driver_version` ≥ 580 → `:cuda13` is an option.
- `compute_cap` ≥ 7.5 (Turing) → supported by both. Below 7.5 (Pascal
  6.1, Volta 7.0) → use `:cuda` only.

## Building a CUDA image yourself

The same Dockerfile builds all variants via `--build-arg PYTORCH_INDEX`:

```bash
docker build --build-arg PYTORCH_INDEX=cu128 -t polyglot-tts:cuda .
docker build --build-arg PYTORCH_INDEX=cu130 -t polyglot-tts:cuda13 .
```

No GPU is needed on the build host — the CUDA driver only matters at
runtime. See [docs/SPARK_BUILD.md](SPARK_BUILD.md) for the local-build
workflow.

## Why not just one CUDA image?

Moving the single `:cuda` tag to cu130 would silently break every user on
Pascal/Volta/Maxwell GPUs and every host pinned below driver 580 (common
in Proxmox/ESXi GPU-passthrough and enterprise/LTS setups). The major
PyTorch-based projects (PyTorch itself, vLLM, ComfyUI) all keep a
CUDA-12 default and offer cu130 as an opt-in — Polyglot follows the same
pattern.
