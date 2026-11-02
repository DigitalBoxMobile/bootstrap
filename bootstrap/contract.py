"""Reference implementation of the client contract (docs/client-contract.md)."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from jwcrypto.jwk import JWK
from jwcrypto.jws import JWS

from bootstrap import manifest as manifest_module
from bootstrap.jws import b64url_decode

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Result:
    accepted: bool
    reason: str | None = None
    manifest: dict | None = None
    valid_kids: list[str] = field(default_factory=list)
    expired: bool = False

    def as_dict(self) -> dict:
        if not self.accepted:
            return {"accepted": False, "reason": self.reason}
        return {
            "accepted": True,
            "env": self.manifest["env"],
            "version": self.manifest["version"],
            "valid_kids": self.valid_kids,
            "expires_at": self.manifest["expires_at"],
            "expired": self.expired,
        }


def _reject(reason: str) -> Result:
    return Result(accepted=False, reason=reason)


def parse_timestamp(text: str) -> datetime:
    return datetime.strptime(text, TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def _parse_envelope(data: bytes) -> tuple[str, list[dict]] | None:
    """Step 1: General JSON envelope with decodable base64url fields."""
    try:
        document = json.loads(data.decode("utf-8"))
        payload = document["payload"]
        signatures = document["signatures"]
        if not isinstance(payload, str) or not isinstance(signatures, list) or not signatures:
            return None
        b64url_decode(payload)
        for sig in signatures:
            if not isinstance(sig["protected"], str) or not isinstance(sig["signature"], str):
                return None
            b64url_decode(sig["protected"])
            b64url_decode(sig["signature"])
    except (ValueError, KeyError, TypeError):
        return None
    return payload, signatures


def _header_kid(protected: str) -> str | None:
    """Step 2: the kid of an eligible signature, or None when it must be skipped."""
    try:
        header = json.loads(b64url_decode(protected))
    except ValueError:
        return None
    if not isinstance(header, dict) or header.get("alg") != "EdDSA" or "crit" in header:
        return None
    kid = header.get("kid")
    return kid if isinstance(kid, str) else None


def _signature_valid(payload: str, sig: dict, jwk: dict) -> bool:
    flattened = {"payload": payload, "protected": sig["protected"], "signature": sig["signature"]}
    try:
        token = JWS()
        token.deserialize(json.dumps(flattened))
        token.verify(JWK(**jwk), alg="EdDSA")
    except Exception:  # any jwcrypto failure means the signature does not verify
        return False
    return True


def evaluate(
    data: bytes,
    *,
    env: str,
    trusted_keys: dict[str, dict],
    stored_version: int = 0,
    now: datetime,
) -> Result:
    envelope = _parse_envelope(data)
    if envelope is None:
        return _reject("malformed")
    payload, signatures = envelope

    candidates = [
        (kid, sig) for sig in signatures if (kid := _header_kid(sig["protected"])) in trusted_keys
    ]
    if not candidates:
        return _reject("unknown_key")
    valid_kids = []
    for kid, sig in candidates:
        if _signature_valid(payload, sig, trusted_keys[kid]) and kid not in valid_kids:
            valid_kids.append(kid)
    if not valid_kids:
        return _reject("bad_signature")

    try:
        parsed = json.loads(b64url_decode(payload).decode("utf-8"))
    except ValueError:
        return _reject("malformed")
    if (
        not isinstance(parsed, dict)
        or not isinstance(parsed.get("schema"), int)
        or isinstance(parsed["schema"], bool)
    ):
        return _reject("malformed")
    if parsed["schema"] != 1:
        return _reject("unknown_schema")
    if manifest_module.schema_errors(parsed):
        return _reject("invalid_manifest")
    try:
        expires_at = parse_timestamp(parsed["expires_at"])
        parse_timestamp(parsed["issued_at"])
    except ValueError:
        return _reject("invalid_manifest")
    if parsed["env"] != env:
        return _reject("env_mismatch")
    if parsed["version"] < stored_version:
        return _reject("rollback")
    return Result(
        accepted=True,
        manifest=parsed,
        valid_kids=valid_kids,
        expired=now >= expires_at,
    )
