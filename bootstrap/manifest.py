"""Manifest loading, canonical serialization and schema validation."""

import json
from datetime import datetime
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator, validators

from bootstrap.jws import b64url_decode

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema" / "manifest.v1.json"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# JSON Schema counts 3.0 as an integer; the client contract does not (docs/client-contract.md).
StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda _, value: isinstance(value, int) and not isinstance(value, bool)
    ),
)


@cache
def validator() -> Draft202012Validator:
    return StrictValidator(json.loads(SCHEMA_PATH.read_text()))


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def compact_bytes(manifest: dict) -> bytes:
    """Canonical payload bytes: compact UTF-8 JSON, key order preserved."""
    return json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def payload_version(dist_bytes: bytes) -> int:
    """`version` of the manifest inside a .jws file; ValueError when unreadable."""
    try:
        version = json.loads(b64url_decode(json.loads(dist_bytes)["payload"]))["version"]
    except (KeyError, TypeError) as error:
        raise ValueError("no manifest version in dist file") from error
    if not isinstance(version, int):
        raise ValueError("no manifest version in dist file")
    return version


def _calendar_errors(manifest: object) -> list[str]:
    errors = []
    for field in ("issued_at", "expires_at"):
        value = manifest.get(field) if isinstance(manifest, dict) else None
        if not isinstance(value, str):
            continue
        try:
            datetime.strptime(value, TIMESTAMP_FORMAT)
        except ValueError:
            errors.append(f"{field}: {value!r} is not a real UTC calendar date")
    return errors


def schema_errors(manifest: object) -> list[str]:
    """JSON Schema violations plus the calendar validity of the timestamps."""
    errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in validator().iter_errors(manifest)
    ]
    return errors or _calendar_errors(manifest)
