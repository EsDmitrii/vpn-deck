<div align="center">
  <img src="assets/logo.png" alt="VPN Deck" width="88" height="88">
  <h1>VPN Deck</h1>
  <p><strong>Paste a link. Pick a server. Flip the toggle.</strong></p>
  <p>VPN right from Steam Deck Game Mode: AmneziaWG, VLESS (REALITY) and v2ray subscriptions.</p>
  <p>
    <a href="#install"><strong>Install</strong></a>
    · <a href="#quick-start">Quick start</a>
    · <a href="README.ru.md">Русский</a>
  </p>
  <img src="assets/screenshot.jpeg" alt="VPN Deck in the Steam Deck quick access menu" width="420">
</div>

## Install

Both ways need Decky's developer mode: Decky Loader → settings → **General** → **Developer mode**.

**From URL.** Decky Loader → settings → **Developer** → **Install Plugin from URL**, type in:

```
https://github.com/mrwaip/vpn-deck/releases/latest/download/vpn-deck.zip
```

**From a file.**

1. Download `vpn-deck.zip` from [Releases](https://github.com/mrwaip/vpn-deck/releases/latest).
2. Copy it to the Deck, e.g. with LocalSend into Downloads, or download it in Desktop Mode.
3. Decky Loader → settings → **Developer** → **Install Plugin from ZIP File** → pick the file.

Turn VPN off in the plugin before updating or removing it.

## Quick start

1. Open the plugin → **Import config**.
2. Paste a link or pick a file:
   - **AmneziaVPN** — export with **Protocol: AmneziaWG**, then:
     - **Connection Format: For the AmneziaVPN app** → a `vpn://` link, paste it;
     - **Connection Format: AmneziaWG native format** → a `.conf` file, pick it.
   - **VLESS** — a `vless://` link or your provider's subscription URL.
   - **sing-box** — a `.json` file.
3. For a subscription, pick a server. You can change it later from the list.
4. Flip the toggle next to the config.

Tunnel up but nothing loads? Re-import the AWG config with **MTU 1280**.

## Disclaimer

**Limitation of liability:** this plugin is a technical tool for managing network configurations (AmneziaWG, VLESS). Use it at your own risk. You are solely responsible for compliance with the laws of your country or region. The developer does not encourage any violation of laws and is not responsible for how or why the plugin is used.

This plugin was developed purely out of enthusiasm, in spare time. If you encounter issues, please open a detailed issue on [GitHub](https://github.com/mrwaip/vpn-deck/issues): describe the steps to reproduce, plugin and system version, and attach logs if necessary.

<sub>BSD-3-Clause · AmneziaWG and sing-box are bundled under their own licenses.</sub>
