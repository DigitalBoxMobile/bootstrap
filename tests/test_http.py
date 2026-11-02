import pytest

from bootstrap import http


@pytest.mark.parametrize("url", ["http://[::1", "https://exa mple.com/x", "ftp://example.com/x"])
def test_invalid_urls_are_unreachable(url):
    response = http.get(url)

    assert response.unreachable


def test_body_over_limit_is_oversized(respx_mock):
    respx_mock.get("https://example.com/x").respond(200, content=b"a" * 70000)

    response = http.get("https://example.com/x")

    assert response.oversized
    assert response.unreachable
    assert response.body == b""


def test_body_at_limit_is_returned(respx_mock):
    respx_mock.get("https://example.com/x").respond(200, content=b"a" * 65536)

    response = http.get("https://example.com/x")

    assert not response.oversized
    assert response.status == 200
    assert len(response.body) == 65536
