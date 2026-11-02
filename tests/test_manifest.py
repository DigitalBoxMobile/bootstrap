import json

import pytest

from bootstrap import jws, manifest


def dist_with_version(version) -> bytes:
    payload = jws.b64url_encode(json.dumps({"version": version}).encode())
    return json.dumps({"payload": payload, "signatures": []}).encode()


def test_payload_version_reads_integer():
    assert manifest.payload_version(dist_with_version(7)) == 7


@pytest.mark.parametrize("version", [True, 3.0, "3", None])
def test_payload_version_rejects_non_integer(version):
    with pytest.raises(ValueError):
        manifest.payload_version(dist_with_version(version))
