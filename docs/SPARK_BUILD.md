# Building for DGX Spark, Jetson, and other ARM64 + NVIDIA hosts

> **You probably don't need this page.** The published
> `ghcr.io/nosdave/polyglot-tts:cuda` image is multi-arch
> (`linux/amd64` + `linux/arm64`) — `docker pull` it on Spark or Jetson
> like you would on an amd64 host. The CI builds the arm64 variant
> natively on GitHub-hosted `ubuntu-24.04-arm` runners since v0.5.2.
>
> This page covers the **local build** workflow: useful if you want a
> custom `PYTORCH_INDEX`, if you're working offline, or if you're
> debugging the image and want to iterate without a CI roundtrip.

## Prerequisites

On the ARM64 + NVIDIA host:

1. **Docker** with BuildKit (default on Docker 20.10+).
2. **NVIDIA Container Toolkit**:
   ```bash
   nvidia-ctk --version            # should print a version
   docker info | grep -i nvidia    # should show 'nvidia' as a runtime
   ```
3. **GPU visible to Docker**:
   ```bash
   docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu22.04 nvidia-smi
   ```
   This should print your GPU. If it errors, fix the container-toolkit
   before continuing.

## Build

One command, runs locally on the ARM host:

```bash
git clone https://github.com/Nosdave/polyglot-tts.git
cd polyglot-tts
docker build --build-arg PYTORCH_INDEX=cu128 -t polyglot-tts:cuda-local .
```

The `:cuda-local` tag is arbitrary — pick whatever you want. Build time
is roughly 10–15 minutes on first run (depends on bandwidth — multi-GB
PyTorch wheels). Subsequent builds use BuildKit cache and finish in under
a minute.

If you prefer a script wrapper:

```bash
./scripts/build-spark.sh                       # tag defaults to polyglot-tts:cuda-local
./scripts/build-spark.sh my-registry/polyglot-tts:0.5.1-cuda-arm64
```

## Verify

```bash
docker run --rm --gpus all polyglot-tts:cuda-local \
  python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

Expected output ends with something like `True NVIDIA GB10` or `True NVIDIA Jetson Orin`.

You may see a startup warning like:

```
WARNING: nvrtc: 'compute_120' is not a valid value for --gpu-architecture
```

This is expected on Blackwell GB10 (compute capability SM 12.1) with
cu128 — newer NVIDIA architectures than the cu128 nvrtc knows about.
The forward path falls back to Triton-compiled kernels and runs at
33–38× real-time on DGX Spark. No production impact.

## Use the local image

In your `docker-compose.yaml`, point the `image:` line at your local tag:

```yaml
services:
  polyglot-tts:
    image: polyglot-tts:cuda-local
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    ports:
      - "10200:10200"
      - "10201:10201"
    environment:
      POCKET_TTS_LANGUAGES: "english_2026-04,german_24l,french_24l"
      POCKET_TTS_VOICE: "eve"
      POCKET_TTS_DEVICE: "cuda"
    volumes:
      - ./voices-extra:/app/voices-extra
      - polyglot-hf-cache:/app/.cache/huggingface

volumes:
  polyglot-hf-cache:
```

If your `voices-extra/` is bind-mounted, make sure UID 10001 (the
container's runtime user) can write to it:

```bash
mkdir -p ./voices-extra
sudo chown -R 10001:10001 ./voices-extra
```

## Why this used to be required (and isn't anymore)

Through v0.5.1, the public `:cuda` image was amd64-only and ARM+CUDA
users had to build locally. GitHub-hosted CI runners were all amd64,
and cross-building `linux/arm64` images via QEMU was slow (30–60 min)
and timed out on multi-GB PyTorch wheel downloads.

In August 2025, GitHub added native `ubuntu-24.04-arm` runners — free
for public repositories, real ARM64 hardware (Neoverse N2), no QEMU.
The CI workflow now builds the arm64 variant natively on those runners
in ~10–15 minutes, and stitches it together with the amd64 variant into
a single multi-arch `:cuda` manifest.

So `docker pull ghcr.io/nosdave/polyglot-tts:cuda` Just Works on
Spark / Jetson / amd64 alike. The local-build workflow above is purely
for special cases (custom build args, offline iteration, your own
registry).
