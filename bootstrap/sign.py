"""`bin/sign`: refresh dates, sign `manifests/<env>.json` into `dist/<env>.jws`."""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jwcrypto.jwk import JWK

from bootstrap import contract, jws, keys
from bootstrap import manifest as manifest_module
from bootstrap.age import Age, AgeError
from bootstrap.errors import CommandError, fail
from bootstrap.manifest import ROOT

VALIDITY = timedelta(days=180)
MAX_DIST_BYTES = 3584


def _check_kids(env: str, kids: list[str], keys_dir: Path) -> dict[str, dict]:
    if len(set(kids)) != len(kids):
        raise CommandError("duplicate_kid", "each --kid may be given once")
    public = {}
    for kid in kids:
        if not keys.KID_RE.fullmatch(kid):
            raise CommandError("invalid_kid", f"{kid!r} does not match {keys.KID_RE.pattern}")
        if keys.env_of_kid(kid) != env:
            raise CommandError("kid_env_mismatch", f"{kid} does not belong to {env}")
        path = keys_dir / f"{kid}.pub.json"
        if not path.is_file():
            raise CommandError("unknown_key", f"{path} does not exist")
        public[kid] = json.loads(path.read_text())
    return public


def _load_private(kid: str, public: dict, key_dir: Path, age: Age) -> JWK:
    path = keys.private_key_path(kid, key_dir)
    if not path.is_file():
        raise CommandError("private_key_missing", f"{path} does not exist")
    try:
        key = JWK(**json.loads(age.decrypt(path)))
    except AgeError as error:
        raise CommandError("age_failed", f"{path}: {error}") from error
    except (ValueError, TypeError) as error:
        raise CommandError("key_mismatch", f"{path} does not hold a private JWK") from error
    if not key.has_private or keys.public_jwk(key, kid) != public:
        raise CommandError("key_mismatch", f"{path} does not match keys/{kid}.pub.json")
    return key


def sign(env: str, kids: list[str], *, root: Path, key_dir: Path, age: Age, now: datetime) -> dict:
    """Sign `env`; `dist/<env>.jws` changes only after every step succeeded."""
    manifest_path = root / "manifests" / f"{env}.json"
    manifest = manifest_module.load(manifest_path)
    manifest["issued_at"] = now.strftime(contract.TIMESTAMP_FORMAT)
    manifest["expires_at"] = (now + VALIDITY).strftime(contract.TIMESTAMP_FORMAT)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    errors = manifest_module.schema_errors(manifest)
    if errors:
        raise CommandError("invalid_manifest", "; ".join(errors))
    if manifest["env"] != env:
        raise CommandError("env_mismatch", f"{manifest_path} has env {manifest['env']!r}")
    public = _check_kids(env, kids, root / "keys")

    dist_path = root / "dist" / f"{env}.jws"
    if dist_path.exists():
        try:
            current = manifest_module.payload_version(dist_path.read_bytes())
        except ValueError as error:
            raise CommandError("dist_unreadable", f"{dist_path}: {error}") from error
        if manifest["version"] <= current:
            raise CommandError(
                "version_not_increased", f"version {manifest['version']} <= signed {current}"
            )

    signers = [(kid, _load_private(kid, public[kid], key_dir, age)) for kid in kids]
    data = jws.sign_general(manifest_module.compact_bytes(manifest), signers)
    if len(data) > MAX_DIST_BYTES:
        raise CommandError("too_large", f"{len(data)} bytes > {MAX_DIST_BYTES}")

    result = contract.evaluate(
        data,
        env=env,
        trusted_keys=keys.trusted_for_env(keys.load_dir(root / "keys"), env),
        now=now,
    )
    if not result.accepted:
        raise CommandError("verify_failed", f"{dist_path}: {result.reason}")
    dist_path.parent.mkdir(exist_ok=True)
    dist_path.write_bytes(data)
    return {"version": manifest["version"], "kids": result.valid_kids, "bytes": len(data)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sign", description=__doc__)
    parser.add_argument("env", choices=["production", "staging"])
    parser.add_argument("--kid", action="append", required=True, dest="kids")
    args = parser.parse_args(argv)

    try:
        result = sign(
            args.env,
            args.kids,
            root=ROOT,
            key_dir=keys.private_key_dir(),
            age=Age(),
            now=datetime.now(UTC).replace(microsecond=0),
        )
    except CommandError as error:
        return fail(error)
    except (OSError, ValueError) as error:
        return fail(CommandError("unreadable_input", str(error)))
    print(
        f"signed {args.env} version {result['version']} with {', '.join(result['kids'])} "
        f"({result['bytes']} bytes); verification passed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
