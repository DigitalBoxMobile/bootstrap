import pytest

from bootstrap import http


@pytest.mark.parametrize("url", ["http://[::1", "https://exa mple.com/x", "ftp://example.com/x"])
def test_invalid_urls_are_unreachable(url):
    response = http.get(url)

    assert response.unreachable
