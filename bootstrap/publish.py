"""`bin/publish`: copy signed `dist/<env>.jws` files to every mirror in `mirrors.yml`."""

import argparse
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from bootstrap import check, contract, keys, mirrors
from bootstrap import http as http_module
from bootstrap import manifest as manifest_module
from bootstrap.age import Age, AgeError
from bootstrap.errors import CommandError, fail
from bootstrap.manifest import ROOT
from bootstrap.mirrors import Target

ENVS = check.ENVS
CONTENT_TYPE = "application/jose+json"
CACHE_CONTROL = "public, max-age=300"
GIT_VERIFY_ATTEMPTS = 10
GIT_VERIFY_PAUSE_S = 30


@dataclass(frozen=True)
class Row:
    target: str
    env: str
    action: str
    result: str


@dataclass(frozen=True)
class Plan:
    env: str
    local: bytes
    action: str  # unchanged | upload | rollback_refused
    problem: str | None = None  # remote could not be read


def failed(rows: list[Row]) -> bool:
    return any(row.result.startswith("error") for row in rows)


def _s3_client(target: Target, access_key: str, secret_key: str):
    return boto3.client(
        "s3",
        endpoint_url=target.endpoint,
        region_name=target.region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


def _run_command(command: list[str], *, env: Mapping[str, str], cwd: Path):
    return subprocess.run(
        command, env=dict(env), cwd=cwd, capture_output=True, text=True, check=False
    )


@dataclass
class Context:
    root: Path
    envs: tuple[str, ...]
    dry_run: bool
    environ: Mapping[str, str]
    s3_factory: Callable
    runner: Callable
    sleep: Callable[[float], None]
    now: datetime
    trusted: dict[str, dict]


def _credentials(ctx: Context, target: Target):
    """(access key, secret) for s3, the deploy key path for git, None when not set."""
    prefix = f"BOOTSTRAP_{target.credentials}_"
    if target.type == "git":
        return ctx.environ.get(f"{prefix}DEPLOY_KEY") or None
    access, secret = (ctx.environ.get(f"{prefix}{name}") for name in _S3_VARS)
    return (access, secret) if access and secret else None


_S3_VARS = ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY")


def _read_remote(ctx: Context, target: Target, env: str, client) -> tuple[bytes | None, str | None]:
    """(bytes, None) | (None, None) when absent | (None, problem code) when unreadable."""
    if client is not None:
        try:
            return client.get_object(Bucket=target.bucket, Key=target.object_key(env))[
                "Body"
            ].read(), None
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None, None
            return None, "read_failed"
        except BotoCoreError:
            return None, "unreachable"
    response = http_module.get(target.public_file_url(env))
    if response.unreachable:
        return None, "unreachable"
    if response.status == 200:
        return response.body, None
    if response.status == 404:
        return None, None
    return None, "http_error"


def _plan(ctx: Context, target: Target, env: str, client) -> Plan:
    local = (ctx.root / "dist" / f"{env}.jws").read_bytes()
    remote, problem = _read_remote(ctx, target, env, client)
    if problem:
        return Plan(env, local, "upload", problem)
    if remote == local:
        return Plan(env, local, "unchanged")
    if remote is not None:
        verdict = contract.evaluate(
            remote,
            env=env,
            trusted_keys=keys.trusted_for_env(ctx.trusted, env),
            now=ctx.now,
        )
        if verdict.accepted and verdict.manifest["version"] >= manifest_module.payload_version(
            local
        ):
            return Plan(env, local, "rollback_refused")
    return Plan(env, local, "upload")


def _verified(target: Target, plan: Plan, ctx: Context, attempts: int) -> bool:
    for attempt in range(attempts):
        if attempt:
            ctx.sleep(GIT_VERIFY_PAUSE_S)
        response = http_module.get(target.public_file_url(plan.env))
        if response.status == 200 and response.body == plan.local:
            return True
    return False


def _write_s3(ctx: Context, target: Target, client, plan: Plan) -> str:
    try:
        client.put_object(
            Bucket=target.bucket,
            Key=target.object_key(plan.env),
            Body=plan.local,
            ContentType=CONTENT_TYPE,
            CacheControl=CACHE_CONTROL,
        )
    except (BotoCoreError, ClientError):
        return "error: write_failed"
    return "ok" if _verified(target, plan, ctx, attempts=1) else "error: verify_failed"


def _git_precheck(ctx: Context) -> str | None:
    status = subprocess.run(
        ["git", "-C", str(ctx.root), "status", "--porcelain", "--", "dist"],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0 or status.stdout.strip():
        return "dist_dirty"
    for env in ctx.envs:
        if (
            check._git_blob(ctx.root, "HEAD", f"dist/{env}.jws")
            != (ctx.root / "dist" / f"{env}.jws").read_bytes()
        ):
            return "dist_not_committed"
    return None


def _write_git(ctx: Context, target: Target, key_path: str, plans: list[Plan]) -> list[str]:
    """Push once; per-plan results (only for the plans that are uploads)."""
    problem = _git_precheck(ctx)
    if problem:
        return [f"error: {problem}"] * len(plans)
    environ = {
        **ctx.environ,
        "GIT_SSH_COMMAND": f"ssh -i {shlex.quote(key_path)} -o IdentitiesOnly=yes",
    }
    pushed = ctx.runner(
        ["git", "push", target.push_url, f"HEAD:refs/heads/{target.branch}"],
        env=environ,
        cwd=ctx.root,
    )
    if pushed.returncode != 0:
        return ["error: push_failed"] * len(plans)
    return [
        "ok" if _verified(target, plan, ctx, GIT_VERIFY_ATTEMPTS) else "warning: stale_cache"
        for plan in plans
    ]


def _publish_target(ctx: Context, target: Target) -> list[Row]:
    credentials = _credentials(ctx, target)
    if credentials is None and not ctx.dry_run:
        return [Row(target.name, env, "-", "error: missing_credentials") for env in ctx.envs]
    client = ctx.s3_factory(target, *credentials) if target.type == "s3" and credentials else None
    plans = [_plan(ctx, target, env, client) for env in ctx.envs]

    def error_of(plan: Plan) -> str | None:
        if plan.action == "rollback_refused":
            return "rollback_refused"
        return None if ctx.dry_run else plan.problem

    if any(error_of(plan) for plan in plans):
        return [
            Row(target.name, p.env, p.action, f"error: {error_of(p)}")
            if error_of(p)
            else Row(target.name, p.env, p.action, "ok" if p.action == "unchanged" else "skipped")
            for p in plans
        ]

    uploads = [p for p in plans if p.action == "upload"]
    if ctx.dry_run:
        results = {p.env: f"warning: {p.problem}" if p.problem else "planned" for p in uploads}
    elif target.type == "s3":
        results = {p.env: _write_s3(ctx, target, client, p) for p in uploads}
    elif uploads:
        results = dict(
            zip(
                (p.env for p in uploads), _write_git(ctx, target, credentials, uploads), strict=True
            )
        )
    else:
        results = {}
    return [Row(target.name, p.env, p.action, results.get(p.env, "ok")) for p in plans]


def run(
    *,
    root: Path,
    targets: list[Target],
    envs: tuple[str, ...] = ENVS,
    target_names: tuple[str, ...] = (),
    dry_run: bool = False,
    environ: Mapping[str, str],
    s3_factory: Callable = _s3_client,
    runner: Callable = _run_command,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> list[Row]:
    now = now or datetime.now(UTC)
    problems = [f"{r.name}: {r.error}" for r in check.run(root=root, now=now) if r.error]
    if problems:
        raise CommandError("check_failed", "; ".join(problems))
    unknown = sorted(set(target_names) - {t.name for t in targets})
    if unknown:
        raise CommandError("unknown_target", ", ".join(unknown))
    ctx = Context(
        root=root,
        envs=tuple(envs),
        dry_run=dry_run,
        environ=environ,
        s3_factory=s3_factory,
        runner=runner,
        sleep=sleep,
        now=now,
        trusted=keys.load_dir(root / "keys"),
    )
    rows: list[Row] = []
    for target in targets:
        if not target_names or target.name in target_names:
            rows.extend(_publish_target(ctx, target))
    return rows


def parse_dotenv(data: bytes) -> dict[str, str]:
    """Simple `KEY=VALUE` lines; `#` comments, blank lines, `export ` and quotes tolerated."""
    values = {}
    for line in data.decode("utf-8").splitlines():
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def load_secrets(path: Path, age) -> dict[str, str]:
    try:
        return parse_dotenv(age.decrypt(path))
    except (AgeError, OSError, UnicodeDecodeError) as error:
        raise CommandError("secrets_unreadable", f"{path}: {error}") from error


def format_table(rows: list[Row]) -> str:
    table = [("target", "env", "action", "result"), *(tuple(vars(r).values()) for r in rows)]
    widths = [max(len(line[i]) for line in table) for i in range(4)]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip()
        for line in table
    )


def main(
    argv: list[str] | None = None,
    *,
    root: Path = ROOT,
    mirrors_path: Path | None = None,
    age=None,
    environ: MutableMapping[str, str] | None = None,
    **run_kwargs,
) -> int:
    parser = argparse.ArgumentParser(prog="publish", description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env", action="append", choices=ENVS, dest="envs")
    parser.add_argument("--target", action="append", dest="target_names")
    parser.add_argument("--secrets", metavar="FILE.age")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ

    try:
        targets = mirrors.load(mirrors_path or root / "mirrors.yml")
        # Check first so a broken repository never reaches the passphrase prompt.
        problems = [f"{r.name}: {r.error}" for r in check.run(root=root) if r.error]
        if problems:
            raise CommandError("check_failed", "; ".join(problems))
        if args.secrets:
            environ.update(load_secrets(Path(args.secrets).expanduser(), age or Age()))
        rows = run(
            root=root,
            targets=targets,
            envs=tuple(args.envs or ENVS),
            target_names=tuple(args.target_names or ()),
            dry_run=args.dry_run,
            environ=environ,
            **run_kwargs,
        )
    except CommandError as error:
        return fail(error)
    print(format_table(rows))
    return 1 if failed(rows) else 0


if __name__ == "__main__":
    sys.exit(main())
