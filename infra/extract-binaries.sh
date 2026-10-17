#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="vpn-deck-builder:local"

# Steam Deck is x86_64; without --platform an Apple Silicon host builds arm64.
# SKIP_IMAGE_BUILD=1 reuses an image built earlier (CI builds it with a cache).
if [[ -z "${SKIP_IMAGE_BUILD:-}" ]]; then
    docker build --platform linux/amd64 -f "$SCRIPT_DIR/Dockerfile" -t "$IMAGE" "$SCRIPT_DIR/.."
fi
docker run --rm --platform linux/amd64 -v "./bin:/out" "$IMAGE" sh -c "cp /binaries/amneziawg-go /binaries/awg /binaries/sing-box /out/"

echo "==> Binaries extracted to ./bin/"
ls -lh ./bin/
