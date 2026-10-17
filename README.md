# VPN Deck

[Русский](README.ru.md)

A Decky Loader plugin for managing VPN connections (AmneziaWG and VLESS) on Steam Deck from gaming mode UI.

![Screenshot](assets/screenshot.jpeg)

## Disclaimer

**Limitation of liability:** this plugin is a technical tool for managing network configurations (AmneziaWG/WireGuard configs). Use it at your own risk. You are solely responsible for compliance with the laws of your country or region. The developer does not encourage any violation of laws and is not responsible for how or why the plugin is used.

This plugin was developed purely out of enthusiasm, in spare time. If you encounter issues, please open a detailed issue on [GitHub](https://github.com/mrwaip/vpn-deck/issues): describe the steps to reproduce, plugin and system version, and attach logs if necessary.

## 📋 Description

**VPN Deck** — VPN right from Steam Deck gaming mode:

- Import: AmneziaWG `.conf`, `vpn://` and `vless://` links, sing-box JSON
- One toggle per config, one config at a time
- Status: exit IP, speed, handshake
- Survives sleep and Wi-Fi changes, reconnects after reboot

Requires root.

### ⚠️ Important: Limitations

**In version 2, the plugin does not manage the `awg0` interface.** Only configs added through the plugin are managed (interfaces named `vd-<name>`).

Configs imported through the plugin are stored in `~/.local/share/vpn-deck/configs`; AWG configs get symlinks in `/etc/amnezia/amneziawg/`. AmneziaWG and sing-box binaries are included in the plugin release — no separate installation required.

## Changes between v1 and v2

| | v1 | v2 |
|---|----|----|
| **Interface** | Managed only one `awg0` interface via `systemctl` | Does **not** manage `awg0`. Only configs added through the plugin (`vd-*` interfaces) |
| **Setup** | Config had to be set up manually in Desktop Mode (building amneziawg-go/awg-quick, creating `awg0.conf`, symlinks) | Configs are imported from UI (`.conf` file). Binaries are included in the release |
| **Configs** | One config (`awg0`) | Multiple configs named `vd-<name>` |

If you were using v1 with `awg0`, after upgrading to v2 the plugin will no longer bring up or stop that interface. To manage VPN through the plugin, re-import your config via "Import config".

## 📥 Installation

Install the plugin **only from official releases** on GitHub.

**Before removing the plugin or installing a new version, turn off VPN in the plugin itself** (set the toggle next to the active config to "off"). Otherwise, the update or removal may fail.

**From URL (easiest):** Decky Loader → settings → "Developer" → "Install Plugin from URL" → paste

```
https://github.com/mrwaip/vpn-deck/releases/latest/download/vpn-deck.zip
```

**From a file:** download `vpn-deck.zip` from [Releases](https://github.com/mrwaip/vpn-deck/releases), copy it to your Steam Deck, open Decky Loader → plugin settings → "Install plugin" → specify the path to the file.

**After installation:** open the plugin in gaming mode → "Import config" → select a file (e.g., from the Downloads folder). After import, the config will appear in the list and can be toggled on/off.

### Which file you need

- AmneziaVPN: a `vpn://` link (save it as `.txt`) or an "AmneziaWG native format" export (`.conf`)
- VLESS: a `vless://` link in a `.txt`
- sing-box: a `.json` with one `tun`

### How to transfer a config to Steam Deck

You need to transfer the file to the Deck in order to select it in the plugin:

- **LocalSend** — install the app on your phone/PC and on the Deck (from Discover in Desktop Mode). Send the file to the Deck; it will land in Downloads. A link can be sent as text and saved as `.txt`.
- **Desktop Mode + browser** — switch to Desktop Mode, open a browser, download the config (or save it from email/messenger) to the Downloads folder. In gaming mode, specify the path to this file in the plugin (e.g., `/home/deck/Downloads/name.conf`).

## Usage

- **Import config** — "Import config" button, select a file. The plugin shows what it is (AWG, VLESS REALITY…). Config name: up to 12 characters (letters, digits, `_`, `=`, `+`, `.`, `-`).
- **MTU** — if the tunnel is up but nothing loads, re-import the config with "MTU 1280".
- **Enable/disable** — toggle next to the config name.
- **Delete config** — "Delete config" under the desired config (the interface will be stopped).
- **Errors** — "Errors" section: view history and clear it with "Clear error history".

## Security

The plugin runs as root, so `PostUp`/`PreUp` commands from `vpn://` links are refused, and sing-box JSON may not open network ports or write files.

## Development

`just test` — unit tests, `just typecheck` — pyright and tsc, `just test-e2e` — e2e in Docker with the real binaries.

## Support & License

For issues or questions — [open an issue on GitHub](https://github.com/mrwaip/vpn-deck/issues). License: BSD-3-Clause.
