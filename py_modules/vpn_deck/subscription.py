"""v2ray-style subscriptions: a URL that returns share links (usually base64).

Stored config (ConfigKind.SUBSCRIPTION) is a small JSON: the URL, the server
the user picked (by its `#name`) and the last link seen for it. Before each
connect the subscription is fetched again and the server's current link is
used, so rotated keys keep working; if fetching fails, the stored link is.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Callable, List, Optional
from urllib.parse import unquote

from . import defaults
from .errors import ConfigImportError
from .runner import CommandRunner

Fetcher = Callable[[str], str]
SUPPORTED_SCHEMES = ("vless://",)


@dataclass(frozen=True)
class Server:
    name: str
    link: str


@dataclass(frozen=True)
class SubscriptionConfig:
    url: str
    server: str
    link: str

    def dumps(self) -> str:
        return json.dumps({"url": self.url, "server": self.server, "link": self.link}, ensure_ascii=False) + "\n"

    @classmethod
    def loads(cls, text: str) -> SubscriptionConfig:
        obj = json.loads(text)
        return cls(url=str(obj["url"]), server=str(obj["server"]), link=str(obj["link"]))


def is_subscription_url(text: str) -> bool:
    return re.fullmatch(r"https?://\S+", text.strip()) is not None


def parse_subscription(body: str) -> List[Server]:
    """Share links from a subscription body (base64 or plain, one per line).
    Unsupported schemes are skipped."""
    text = body.strip()
    if not re.match(r"^[a-z0-9]+://", text):
        # Panels use either the standard or the URL-safe alphabet.
        compact = re.sub(r"\s+", "", text).replace("-", "+").replace("_", "/")
        try:
            text = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            raise ConfigImportError("Подписка не в формате v2ray (ожидался список ссылок или base64)")
    servers: List[Server] = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith(SUPPORTED_SCHEMES):
            name = unquote(line.split("#", 1)[1]).strip() if "#" in line else ""
            servers.append(Server(name=name or f"Сервер {len(servers) + 1}", link=line))
    if not servers:
        raise ConfigImportError("В подписке нет серверов VLESS")
    return servers


def find_server(servers: List[Server], name: str) -> Optional[Server]:
    return next((s for s in servers if s.name == name), None)


def curl_fetcher(runner: CommandRunner) -> Fetcher:
    def fetch(url: str) -> str:
        if not is_subscription_url(url):
            raise ConfigImportError("Ссылка на подписку должна начинаться с http:// или https://")
        r = runner.run(["curl", "-sSfL", "--max-time", "15", "--max-filesize", str(defaults.MAX_IMPORT_BYTES),
                        "-A", defaults.SUBSCRIPTION_USER_AGENT, url], timeout=20)
        if not r.ok:
            raise ConfigImportError(f"Не удалось скачать подписку: {r.stderr.strip() or f'curl rc={r.returncode}'}")
        return r.stdout
    return fetch


def refresh(cfg: SubscriptionConfig, fetch: Fetcher) -> SubscriptionConfig:
    """Current link for the stored server; raises if fetching or lookup fails."""
    server = find_server(parse_subscription(fetch(cfg.url)), cfg.server)
    if server is None:
        raise ConfigImportError(f"Сервера «{cfg.server}» больше нет в подписке")
    return SubscriptionConfig(url=cfg.url, server=cfg.server, link=server.link)
