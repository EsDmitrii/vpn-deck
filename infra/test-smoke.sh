#!/usr/bin/env bash
# Unit tests inside a Steam Deck OS container (holo-base), with its Python.
set -euo pipefail

IMAGE="ghcr.io/steamdeckhomebrew/holo-base:latest"

echo "==> Running unit tests in ${IMAGE}"

docker run --rm \
    --platform linux/amd64 \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$(pwd):/plugin:ro" \
    -w /plugin \
    "$IMAGE" \
    python3 -m unittest discover -s tests -t .
