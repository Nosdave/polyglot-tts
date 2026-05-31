# Building for DGX Spark, Jetson, and other ARM64 + NVIDIA hosts

The published `:cuda` image is `linux/amd64` only. For ARM64 hosts with
NVIDIA GPUs — NVIDIA DGX Spark (Grace + Blackwell GB10), Jetson Orin/AGX,
Ampere ARM workstations — you build a local image directly on the host.

This isn't a workaround for a missing feature; it's faster and more
reliable than emulated cross-builds on GitHub-hosted CI runners.

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

## Why CI doesn't ship an ARM64 CUDA image

GitHub-hosted CI runners are amd64. Building `linux/arm64` images on
amd64 needs QEMU emulation, which:

- Slows the build from ~5 min to 30–60 min, and
- Frequently times out during multi-GB PyTorch wheel downloads.

A native build on the actual ARM+CUDA target completes in 10–15 minutes
with no flakiness — so for the small population of users on ARM64+CUDA
hosts (Spark, Jetson), a local build is the right answer.

If you're maintaining a fleet and want a pull-able image, push the
locally-built image to your own registry (GHCR, Docker Hub, internal
Harbor, etc.):

```bash
docker tag polyglot-tts:cuda-local ghcr.io/<your-org>/polyglot-tts:0.5.1-cuda-arm64
docker push ghcr.io/<your-org>/polyglot-tts:0.5.1-cuda-arm64
```
