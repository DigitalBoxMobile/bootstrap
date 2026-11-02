"""`bin/check-mirrors`: compare every mirror URL of the reference manifest with `dist/`."""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bootstrap import contract, keys
from bootstrap import http as http_module
from bootstrap import manifest as manifest_module
from bootstrap.errors import CommandError, fail
from bootstrap.manifest import ROOT
from bootstrap.verify import parse_now

ENVS = tuple(keys.PREFIX_ENV.values())
DEFAULT_WARN_DAYS = 60


@dataclass(frozen=True)
class Row:
    env: str
    url: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        return {"env": self.env, "url": self.url, "ok": self.ok, "problems": self.problems}


def _trusted(root: Path, env: str) -> dict[str, dict]:
    try:
        return keys.trusted_for_env(keys.load_dir(root / "keys"), env)
    except (OSError, ValueError, KeyError) as error:
        raise CommandError("bad_key", f"keys/: {error}") from error


def _reference(root: Path, env: str, trusted: dict[str, dict], now: datetime):
    path = root / "dist" / f"{env}.jws"
    try:
        data = path.read_bytes()
    except OSError as error:
        raise CommandError("dist_missing", f"{path}: {error.strerror}") from error
    result = contract.evaluate(data, env=env, trusted_keys=trusted, now=now)
    if not result.accepted:
        raise CommandError("reference_invalid", f"{path.name}: {result.reason}")
    return data, result.manifest


def _mismatch_code(remote: bytes, reference: bytes) -> str:
    try:
        remote_version = manifest_module.payload_version(remote)
        reference_version = manifest_module.payload_version(reference)
    except ValueError:
        return "content_mismatch"
    if remote_version < reference_version:
        return "version_behind"
    if remote_version > reference_version:
        return "version_ahead"
    return "content_mismatch"


def _check_url(
    url: str, *, env: str, reference: bytes, trusted: dict[str, dict], now: datetime, expiring: bool
) -> Row:
    problems = []
    response = http_module.get(url)
    if response.unreachable:
        problems.append("unreachable")
    elif response.status != 200:
        problems.append("http_error")
    else:
        result = contract.evaluate(response.body, env=env, trusted_keys=trusted, now=now)
        if not result.accepted:
            problems.append(result.reason)
        elif response.body != reference:
            problems.append(_mismatch_code(response.body, reference))
    if expiring:
        problems.append("expiring")
    return Row(env, url, problems)


def run(
    *,
    root: Path,
    envs: tuple[str, ...] = ENVS,
    now: datetime,
    warn_days: int = DEFAULT_WARN_DAYS,
) -> list[Row]:
    """Raises CommandError when a reference file is missing or does not verify."""
    rows = []
    for env in envs:
        trusted = _trusted(root, env)
        reference, manifest = _reference(root, env, trusted, now)
        remaining = contract.parse_timestamp(manifest["expires_at"]) - now
        expiring = remaining <= timedelta(days=warn_days)
        rows.extend(
            _check_url(
                url, env=env, reference=reference, trusted=trusted, now=now, expiring=expiring
            )
            for url in manifest["mirrors"]
        )
    return rows


def format_text(rows: list[Row]) -> str:
    return "\n".join(
        f"ok    {r.env}  {r.url}" if r.ok else f"FAIL  {r.env}  {r.url}  {', '.join(r.problems)}"
        for r in rows
    )


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(prog="check-mirrors", description=__doc__)
    parser.add_argument("--env", action="append", choices=ENVS, dest="envs")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--expire-warn-days", type=int, default=DEFAULT_WARN_DAYS)
    parser.add_argument("--now", type=parse_now, default=None)
    args = parser.parse_args(argv)

    try:
        rows = run(
            root=root,
            envs=tuple(args.envs or ENVS),
            now=args.now or datetime.now(UTC),
            warn_days=args.expire_warn_days,
        )
    except CommandError as error:
        return fail(error)
    if args.format == "json":
        print(json.dumps([r.as_dict() for r in rows], ensure_ascii=False))
    else:
        print(format_text(rows))
    return 0 if all(r.ok for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
