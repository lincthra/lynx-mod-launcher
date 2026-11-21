"""Tiny HTTP(S) client on top of urllib, so the frozen exe has no third-party runtime deps
(certifi is used for extra CA roots when it's bundled)."""

from __future__ import annotations

import ssl
import urllib.request
from typing import Callable

from . import __version__

USER_AGENT = f"LynxModLauncher/{__version__}"


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi  # bundled in the PyInstaller build; frozen Linux builds may not find distro CAs

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx


class HttpFetcher:
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self._ctx = _ssl_context()

    def _open(self, url: str, headers: dict[str, str] | None = None):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        if url.lower().startswith("https:"):
            return urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx)
        return urllib.request.urlopen(req, timeout=self.timeout)

    def get_bytes(self, url: str, limit: int = 32 * 1024 * 1024) -> bytes:
        with self._open(url, {"Cache-Control": "no-cache", "Pragma": "no-cache"}) as resp:
            data = resp.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"{url} is larger than {limit} bytes")
        return data

    def download(self, url: str, sink: Callable[[bytes], None], chunk: int = 256 * 1024) -> None:
        with self._open(url) as resp:
            while True:
                block = resp.read(chunk)
                if not block:
                    break
                sink(block)
