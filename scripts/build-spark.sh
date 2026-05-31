#!/usr/bin/env bash
# Build polyglot-tts for ARM64 + NVIDIA CUDA on the target host directly.
#
# Use this on:
#   - NVIDIA DGX Spark (Grace + Blackwell GB10)
#   - NVIDIA Jetson Orin / AGX
#   - Ampere-Altra-class ARM workstations with NVIDIA GPUs
#
# Why local: GitHub-hosted CI runners are amd64. Cross-building
# linux/arm64 via QEMU is slow (30-60 min) and times out on multi-GB
# wheel downloads. A native build on the actual ARM+CUDA host
# finishes in 10-15 minutes.
#
# See docs/SPARK_BUILD.md for full context, verification, and the
# Compose snippet to use the resulting image.

set -euo pipefail

IMAGE_TAG="${1:-polyglot-tts:cuda-local}"
PYTORCH_INDEX="${PYTORCH_INDEX:-cu128}"

echo "Building polyglot-tts for ARM64+CUDA"
echo "  Tag:            ${IMAGE_TAG}"
echo "  PYTORCH_INDEX:  ${PYTORCH_INDEX}"
echo "  Host arch:      $(uname -m)"
echo

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not on PATH" >&2
    exit 1
fi

if ! docker info 2>/dev/null | grep -qi nvidia; then
    echo "WARNING: 'nvidia' runtime not detected in 'docker info'." >&2
    echo "         Install NVIDIA Container Toolkit before running the image." >&2
    echo
fi

docker build \
    --build-arg "PYTORCH_INDEX=${PYTORCH_INDEX}" \
    -t "${IMAGE_TAG}" \
    .

cat <<EOF

Built: ${IMAGE_TAG}

Test GPU access:
  docker run --rm --gpus all ${IMAGE_TAG} \\
    python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"

Reference the image in your docker-compose.yaml as:
  image: ${IMAGE_TAG}

Full setup: docs/SPARK_BUILD.md
EOF
