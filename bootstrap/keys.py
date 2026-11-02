"""Public key directory: kid format, kid -> environment mapping, strict JWK loading."""

import json
import os
import re
from pathlib import Path

from jwcrypto.jwk import JWK

from bootstrap.errors import CommandError
from bootstrap.jws import b64url_decode
from bootstrap.manifest import ROOT

KID_RE = re.compile(r"^(prod|stage)-[0-9]{4}[a-z]$")
PREFIX_ENV = {"prod": "production", "stage": "staging"}
ENVS = tuple(PREFIX_ENV.values())
DEFAULT_DIR = ROOT / "keys"
PUBLIC_SUFFIX = ".pub.json"
PRIVATE_MEMBERS = {"d", "p", "q", "dp", "dq", "qi", "oth", "k"}  # RFC 7518 private JWK members


def env_of_kid(kid: str) -> str:
    if not KID_RE.fullmatch(kid):
        raise ValueError(f"invalid kid: {kid!r}")
    return PREFIX_ENV[kid.split("-", 1)[0]]


def read_public(path: Path) -> dict:
    """A public Ed25519 signing JWK whose kid matches the file name; CommandError otherwise."""
    try:
        jwk = json.loads(path.read_text())
        x = b64url_decode(jwk["x"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise CommandError("bad_key", f"{path.name}: not a readable public JWK") from error
    kid = path.name.removesuffix(PUBLIC_SUFFIX)
    problems = []
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or len(x) != 32:
        problems.append("not an Ed25519 OKP key")
    if jwk.get("use") != "sig" or jwk.get("alg") != "EdDSA":
        problems.append('use must be "sig" and alg "EdDSA"')
    if private := sorted(PRIVATE_MEMBERS & jwk.keys()):
        problems.append(f"contains private members {', '.join(private)}")
    if jwk.get("kid") != kid or not KID_RE.fullmatch(kid):
        problems.append(f"kid {jwk.get('kid')!r} does not match the file name")
    if problems:
        raise CommandError("bad_key", f"{path.name}: {', '.join(problems)}")
    return jwk


def load_dir(directory: Path = DEFAULT_DIR) -> dict[str, dict]:
    """Every `*.pub.json` JWK, validated by `read_public`, keyed by kid."""
    return {
        jwk["kid"]: jwk
        for jwk in map(read_public, sorted(Path(directory).glob(f"*{PUBLIC_SUFFIX}")))
    }


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
    return {kid: jwk for kid, jwk in keys.items() if env_of_kid(kid) == env}
