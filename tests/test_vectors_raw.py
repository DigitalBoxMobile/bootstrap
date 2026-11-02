"""Verify test vectors with raw Ed25519 only: no jwcrypto, no bootstrap package.

Proves that the client contract is implementable with a plain crypto primitive.
"""

import base64
import json
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

TESTDATA = Path(__file__).parent.parent / "testdata"
VECTORS = json.loads((TESTDATA / "vectors.json").read_text())
SIGNATURE_REASONS = {"unknown_key", "bad_signature"}


def b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def trusted_keys(vector: dict) -> dict[str, Ed25519PublicKey]:
    result = {}
    for kid in vector["trusted_kids"]:
        jwk = json.loads((TESTDATA / "keys" / f"{kid}.pub.json").read_text())
        result[kid] = Ed25519PublicKey.from_public_bytes(b64url(jwk["x"]))
    return result


def signature_outcome(vector: dict) -> tuple[str, list[str]]:
    """Return ("unknown_key" | "bad_signature" | "ok", kids with a valid signature)."""
    doc = json.loads((TESTDATA / vector["file"]).read_bytes())
    trusted = trusted_keys(vector)
    eligible, valid = [], []
    for sig in doc["signatures"]:
        header = json.loads(b64url(sig["protected"]))
        if (
            header.get("alg") != "EdDSA"
            or "crit" in header
            or not isinstance(header.get("kid"), str)
        ):
            continue
        if header["kid"] not in trusted:
            continue
        eligible.append(header["kid"])
        signing_input = f"{sig['protected']}.{doc['payload']}".encode("ascii")
        try:
            trusted[header["kid"]].verify(b64url(sig["signature"]), signing_input)
        except InvalidSignature:
            continue
        valid.append(header["kid"])
    if not eligible:
        return "unknown_key", []
    return ("ok" if valid else "bad_signature"), valid


@pytest.mark.parametrize(
    "vector", [v for v in VECTORS if v["name"] != "malformed"], ids=lambda v: v["name"]
)
def test_signature_outcome_matches_expectation(vector):
    outcome, valid = signature_outcome(vector)
    expect = vector["expect"]
    if not expect["accepted"] and expect["reason"] in SIGNATURE_REASONS:
        assert outcome == expect["reason"]
    else:
        assert outcome == "ok"
        assert valid


@pytest.mark.parametrize(
    "vector", [v for v in VECTORS if v["expect"]["accepted"]], ids=lambda v: v["name"]
)
def test_accepted_payload_fields(vector):
    doc = json.loads((TESTDATA / vector["file"]).read_bytes())
    manifest = json.loads(b64url(doc["payload"]))
    assert manifest["schema"] == 1
    assert manifest["env"] == vector["env"]
    assert manifest["version"] == vector["expect"]["version"]
    assert manifest["version"] >= vector["stored_version"]
    expired = vector["now"].replace("Z", "+00:00") >= manifest["expires_at"].replace("Z", "+00:00")
    assert expired == vector["expect"]["expired"]
