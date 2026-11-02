"""JWS helpers: base64url and General JSON Serialization signing (jwcrypto)."""

import base64
import binascii
import json
import re

from jwcrypto.jwk import JWK
from jwcrypto.jws import JWS

_B64URL = re.compile(r"[A-Za-z0-9_-]*")


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Strict unpadded base64url; raises ValueError on any deviation."""
    if not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError("invalid base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error as error:
        raise ValueError("invalid base64url") from error


def protected_header(kid: str) -> str:
    return json.dumps({"alg": "EdDSA", "kid": kid}, separators=(",", ":"))


def raw_sign(key: JWK, signing_input: bytes) -> bytes:
    """Plain Ed25519 signature over `signing_input` (for crafting test inputs)."""
    return key.get_op_key("sign").sign(signing_input)


def sign_general(payload: bytes, signers: list[tuple[str, JWK]]) -> bytes:
    """Build a General JSON JWS file (compact JSON + newline), even for one signature."""
    signatures = []
    encoded_payload = b64url_encode(payload)
    for kid, key in signers:
        token = JWS(payload)
        token.add_signature(key, alg="EdDSA", protected=protected_header(kid))
        flattened = json.loads(token.serialize())  # single signature: flattened form
        signatures.append(
            {"protected": flattened["protected"], "signature": flattened["signature"]}
        )
    document = {"payload": encoded_payload, "signatures": signatures}
    return json.dumps(document, separators=(",", ":")).encode("ascii") + b"\n"
