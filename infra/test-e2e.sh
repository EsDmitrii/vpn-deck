#!/usr/bin/env bash
# End-to-end tests against the real binaries in a privileged container.
# Needs the builder image (just build-image, or IMAGE=... for a pulled one).
set -euo pipefail

IMAGE="${IMAGE:-vpn-deck-builder:local}"

docker run --rm --privileged --platform linux/amd64 \
    -v "$(pwd):/src:ro" \
    "$IMAGE" \
    bash /src/tests/e2e/run.sh
