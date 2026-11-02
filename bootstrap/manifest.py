"""Manifest loading, canonical serialization and schema validation."""

import json
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema" / "manifest.v1.json"


@cache
def validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def compact_bytes(manifest: dict) -> bytes:
    """Canonical payload bytes: compact UTF-8 JSON, key order preserved."""
    return json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def schema_errors(manifest: object) -> list[str]:
    return [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in validator().iter_errors(manifest)
    ]
