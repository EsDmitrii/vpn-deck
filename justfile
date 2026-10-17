image := "ghcr.io/mrwaip/vpn-deck-builder"
tag := "latest"

# Show available recipes
default:
    @just --list

# Install JS dependencies
install:
    pnpm install

# Unit tests (no root, no binaries)
test:
    python3 -m unittest discover -s tests -t .

# Unit tests inside a Steam Deck OS container (holo-base, its Python)
test-holo:
    bash infra/test-smoke.sh

# End-to-end: real awg-quick / amneziawg-go / sing-box in a privileged container
test-e2e: build-image-local
    bash infra/test-e2e.sh

# Type checks: Python (pyright strict) and TypeScript
typecheck:
    pyright
    pnpm exec tsc --noEmit

# Everything CI runs
check: typecheck test test-e2e

# Build the builder image locally (used by e2e and build-binaries)
build-image-local:
    docker build --platform linux/amd64 -t vpn-deck-builder:local -f infra/Dockerfile .

# Build the builder Docker image
build-image:
    docker build -t {{ image }}:{{ tag }} -f infra/Dockerfile .

# Push the builder image to GHCR
push-image: build-image
    docker push {{ image }}:{{ tag }}

# Extract AmneziaWG and sing-box binaries from Docker image to ./bin/
build-binaries:
    bash infra/extract-binaries.sh

# Build plugin zip (copies binaries to bin/, packs via Decky CLI)
build-plugin:
    bash infra/build-plugin.sh

# Release (bump version, build, publish GitHub release)
release bump="patch":
    pnpm release:{{ bump }}
