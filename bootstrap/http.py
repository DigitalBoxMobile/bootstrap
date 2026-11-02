"""Shared HTTP GET with a timeout; network failures become a status-less response."""

from dataclasses import dataclass

import httpx

DEFAULT_TIMEOUT = 10.0


@dataclass(frozen=True)
class Response:
    """`status` is None when the host was unreachable (DNS, TLS, connect, timeout)."""

    status: int | None
    body: bytes = b""

    @property
    def unreachable(self) -> bool:
        return self.status is None


def get(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> Response:
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError:
        return Response(None)
    return Response(response.status_code, response.content)
