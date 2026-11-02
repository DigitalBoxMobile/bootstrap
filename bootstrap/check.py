"""`bin/check`: repository consistency checks for CI and local use (no network)."""

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bootstrap import contract, keys
from bootstrap import manifest as manifest_module
from bootstrap.errors import CommandError, fail
from bootstrap.jws import b64url_decode
from bootstrap.manifest import ROOT
from bootstrap.sign import MAX_DIST_BYTES

ENVS = tuple(keys.PREFIX_ENV.values())
IGNORED_KEY_FILES = {".gitkeep"}


@dataclass(frozen=True)
class Result:
    name: str
    error: CommandError | None = None


def _step(results: list[Result], name: str, check: Callable[[], object]) -> object | None:
    """Run one check, record its outcome and return its value (None on failure)."""
    try:
        value = check()
    except CommandError as error:
        results.append(Result(name, error))
        return None
    results.append(Result(name))
    return value if value is not None else True


def _read_key(path: Path) -> dict:
    try:
        jwk = json.loads(path.read_text())
        x = b64url_decode(jwk["x"])
    except (ValueError, KeyError, TypeError) as error:
        raise CommandError("bad_key", f"{path.name}: not a readable public JWK") from error
    kid = path.name.removesuffix(".pub.json")
    problems = []
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or len(x) != 32:
        problems.append("not an Ed25519 OKP key")
    if "d" in jwk:
        problems.append("contains a private scalar `d`")
    if jwk.get("kid") != kid or not keys.KID_RE.fullmatch(kid):
        problems.append(f"kid {jwk.get('kid')!r} does not match the file name")
    if problems:
        raise CommandError("bad_key", f"{path.name}: {', '.join(problems)}")
    return jwk


def _check_keys(root: Path, results: list[Result]) -> dict[str, dict]:
    trusted = {}
    directory = root / "keys"
    for path in sorted(directory.iterdir()) if directory.is_dir() else []:
        if path.name in IGNORED_KEY_FILES:
            continue
        if not path.name.endswith(".pub.json"):
            results.append(
                Result(
                    f"keys/{path.name}",
                    CommandError("unexpected_file", f"keys/{path.name} is not a *.pub.json file"),
                )
            )
            continue
        jwk = _step(results, f"keys/{path.name}", lambda path=path: _read_key(path))
        if jwk:
            trusted[jwk["kid"]] = jwk
    return trusted


def _check_manifest(root: Path, env: str) -> dict:
    path = root / "manifests" / f"{env}.json"
    try:
        manifest = manifest_module.load(path)
    except OSError as error:
        raise CommandError("manifest_missing", f"{path}: {error.strerror}") from error
    except ValueError as error:
        raise CommandError("invalid_manifest", f"{path}: {error}") from error
    errors = manifest_module.schema_errors(manifest)
    if errors:
        raise CommandError("invalid_manifest", f"{path.name}: {'; '.join(errors)}")
    if manifest["env"] != env:
        raise CommandError("env_mismatch", f"{path.name} has env {manifest['env']!r}")
    issued = contract.parse_timestamp(manifest["issued_at"])
    if contract.parse_timestamp(manifest["expires_at"]) <= issued:
        raise CommandError("expiry_order", f"{path.name}: expires_at is not after issued_at")
    return manifest


def _check_dist(
    root: Path, env: str, manifest: dict | None, trusted: dict[str, dict], now: datetime
) -> None:
    path = root / "dist" / f"{env}.jws"
    if not path.is_file():
        raise CommandError("dist_missing", f"{path} does not exist")
    data = path.read_bytes()
    if len(data) > MAX_DIST_BYTES:
        raise CommandError("too_large", f"{path.name}: {len(data)} bytes > {MAX_DIST_BYTES}")
    result = contract.evaluate(
        data, env=env, trusted_keys=keys.trusted_for_env(trusted, env), now=now
    )
    if not result.accepted:
        raise CommandError(result.reason, f"{path.name} does not verify")
    payload = b64url_decode(json.loads(data)["payload"])
    if manifest is not None and payload != manifest_module.compact_bytes(manifest):
        raise CommandError(
            "payload_mismatch", f"{path.name} payload differs from manifests/{env}.json"
        )


def _git_blob(root: Path, ref: str, path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{path}"], capture_output=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def _version(data: bytes, label: str) -> int:
    try:
        return manifest_module.payload_version(data)
    except ValueError as error:
        raise CommandError("dist_unreadable", f"{label}: {error}") from error


def _check_base(root: Path, env: str, ref: str) -> None:
    path = f"dist/{env}.jws"
    old = _git_blob(root, ref, path)
    new = (root / path).read_bytes() if (root / path).is_file() else None
    if old is None:
        if new is not None:
            _version(new, path)
        return
    if new is None:
        raise CommandError("dist_deleted", f"{path} exists in {ref} but not in the working tree")
    if new == old:
        return
    new_version, old_version = _version(new, path), _version(old, f"{ref}:{path}")
    if new_version <= old_version:
        raise CommandError(
            "version_not_increased", f"{path}: version {new_version} <= {old_version} in {ref}"
        )


def run(*, root: Path, base: str | None = None, now: datetime | None = None) -> list[Result]:
    """Run every check; raises CommandError only when `base` is not a commit."""
    now = now or datetime.now(UTC)
    if base is not None:
        probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            raise CommandError("bad_base", f"{base!r} is not a commit")

    results: list[Result] = []
    trusted = _check_keys(root, results)
    for env in ENVS:
        manifest = _step(
            results, f"manifests/{env}.json", lambda env=env: _check_manifest(root, env)
        )
        _step(
            results,
            f"dist/{env}.jws",
            lambda env=env, manifest=manifest: _check_dist(
                root, env, manifest or None, trusted, now
            ),
        )
        if base is not None:
            _step(
                results, f"base {base} dist/{env}.jws", lambda env=env: _check_base(root, env, base)
            )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check", description=__doc__)
    parser.add_argument("--base", metavar="GIT_REF")
    args = parser.parse_args(argv)

    try:
        results = run(root=ROOT, base=args.base)
    except CommandError as error:
        return fail(error)
    failed = False
    for result in results:
        if result.error:
            failed = True
            print(f"error: {result.error}", file=sys.stderr)
        else:
            print(f"ok: {result.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
