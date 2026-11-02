"""Shared HTTP GET with a timeout; network failures become a status-less response."""

from dataclasses import dataclass

import httpx

DEFAULT_TIMEOUT = 10.0
MAX_BODY_BYTES = 65536


@dataclass(frozen=True)
class Response:
    """`status` is None when the host was unreachable (DNS, TLS, connect, timeout)."""

    status: int | None
    body: bytes = b""
    oversized: bool = False

    @property
    def unreachable(self) -> bool:
        return self.status is None


def get(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> Response:
    """Bodies over MAX_BODY_BYTES yield a status-less response with `oversized` set."""
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_BODY_BYTES:
                    return Response(None, oversized=True)
            return Response(response.status_code, bytes(body))
    except (httpx.HTTPError, httpx.InvalidURL):
        return Response(None)
