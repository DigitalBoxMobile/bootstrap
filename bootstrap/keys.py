"""Public key directory: kid format, kid -> environment mapping."""

import json
import os
import re
from pathlib import Path

from jwcrypto.jwk import JWK

from bootstrap.errors import CommandError
from bootstrap.manifest import ROOT

KID_RE = re.compile(r"^(prod|stage)-[0-9]{4}[a-z]$")
PREFIX_ENV = {"prod": "production", "stage": "staging"}
DEFAULT_DIR = ROOT / "keys"


def env_of_kid(kid: str) -> str:
    if not KID_RE.fullmatch(kid):
        raise ValueError(f"invalid kid: {kid!r}")
    return PREFIX_ENV[kid.split("-", 1)[0]]


def load_dir(directory: Path = DEFAULT_DIR) -> dict[str, dict]:
    """Load every `*.pub.json` JWK, keyed by kid."""
    keys = {}
    for path in sorted(Path(directory).glob("*.pub.json")):
        try:
            jwk = json.loads(path.read_text())
            keys[jwk["kid"]] = jwk
        except (ValueError, KeyError, TypeError) as error:
            raise CommandError("bad_key", f"{path.name}: not a public JWK with a kid") from error
    return keys


def public_jwk(key: JWK, kid: str) -> dict:
    """Public half of an Ed25519 key in the shape of `keys/<kid>.pub.json`."""
    public = json.loads(key.export_public())
    return {
        "kty": public["kty"],
        "crv": public["crv"],
        "x": public["x"],
        "kid": kid,
        "use": "sig",
        "alg": "EdDSA",
    }


def private_key_dir() -> Path:
    override = os.environ.get("BOOTSTRAP_KEY_DIR")
    return Path(override).expanduser() if override else Path.home() / ".config/konsierge/bootstrap"


def private_key_path(kid: str, directory: Path | None = None) -> Path:
    return (directory or private_key_dir()) / f"{kid}.jwk.age"


def trusted_for_env(keys: dict[str, dict], env: str) -> dict[str, dict]:
    return {
        kid: jwk for kid, jwk in keys.items() if KID_RE.fullmatch(kid) and env_of_kid(kid) == env
    }
