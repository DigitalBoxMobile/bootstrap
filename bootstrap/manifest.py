"""Manifest loading, canonical serialization and schema validation."""

import json
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator, validators

from bootstrap.jws import b64url_decode

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema" / "manifest.v1.json"

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


def schema_errors(manifest: object) -> list[str]:
    return [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in validator().iter_errors(manifest)
    ]
