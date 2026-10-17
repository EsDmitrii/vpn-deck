#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-}"

echo "==> Building plugin"

./infra/extract-binaries.sh
./cli/decky plugin build

# out/vpn-deck.zip keeps a fixed name so
# https://github.com/mrwaip/vpn-deck/releases/latest/download/vpn-deck.zip
# always points at the latest release (Decky: "Install Plugin from URL").
if [[ -n "$VERSION" ]]; then
    cp ./out/vpn-deck.zip "./out/vpn-deck-v${VERSION}.zip"
    echo "==> Also saved as vpn-deck-v${VERSION}.zip"
fi
