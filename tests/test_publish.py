import shlex
import subprocess
from pathlib import Path

import boto3
import botocore.exceptions
import httpx
import pytest
import yaml
from moto import mock_aws

from bootstrap import check, publish
from bootstrap.errors import CommandError
from bootstrap.mirrors import Target
from tests.conftest import Repo

PUBLIC = "https://pub.example/v1/"
GIT_PUBLIC = "https://raw.example/dist/"
S3_ENV = {
    "BOOTSTRAP_YANDEX_ACCESS_KEY_ID": "key",
    "BOOTSTRAP_YANDEX_SECRET_ACCESS_KEY": "secret",
}
DEPLOY_KEY = b"-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----"
BOTH_ENVS = ("production", "staging")

S3_TARGET = Target(
    name="yandex",
    type="s3",
    public_url=PUBLIC,
    credentials="YANDEX",
    endpoint="https://s3.example",
    region="ru-central1",
    bucket="bucket",
    prefix="v1/",
)
GIT_TARGET = Target(
    name="github",
    type="git",
    public_url=GIT_PUBLIC,
    credentials="GITHUB",
    push_url="git@example.com:o/r.git",
    branch="master",
    known_hosts="known_hosts",
)


class FakeRunner:
    def __init__(self, returncode: int = 0, stderr: str = "rejected"):
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict, Path]] = []
        self.ssh: list[str] = []
        self.key_mode: int | None = None
        self.key_content: bytes | None = None

    def __call__(self, command, *, env, cwd):
        self.calls.append((command, env, cwd))
        if "GIT_SSH_COMMAND" in env:
            self.ssh = shlex.split(env["GIT_SSH_COMMAND"])
            key = Path(self.ssh[self.ssh.index("-i") + 1])
            self.key_mode = key.stat().st_mode & 0o777
            self.key_content = key.read_bytes()
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    key = tmp_path / "deploy-key"
    key.write_bytes(DEPLOY_KEY)
    key.chmod(0o644)  # GitLab File variables are not guaranteed to be 0600
    return {"BOOTSTRAP_GITHUB_DEPLOY_KEY": str(key)}


@pytest.fixture
def signed(repo: Repo) -> Repo:
    repo.sign_all()
    repo.commit("signed")
    return repo


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="bucket")
        yield client


@pytest.fixture
def sleeps() -> list[float]:
    return []


def objects(s3) -> list[str]:
    return [o["Key"] for o in s3.list_objects_v2(Bucket="bucket").get("Contents", [])]


def run(repo: Repo, targets, *, s3=None, runner=None, sleeps=None, **kwargs):
    kwargs.setdefault("environ", {})
    return publish.run(
        root=repo.root,
        targets=targets,
        s3_factory=(lambda target, key, secret: s3),
        runner=runner or FakeRunner(),
        sleep=(sleeps.append if sleeps is not None else (lambda seconds: None)),
        **kwargs,
    )


def rows_of(rows):
    return [(r.target, r.env, r.action, r.result) for r in rows]


def test_upload_writes_object_and_verifies(signed, s3, respx_mock):
    local = signed.dist_path("production").read_bytes()
    respx_mock.get(f"{PUBLIC}production.jws").mock(side_effect=[httpx.Response(200, content=local)])

    rows = run(signed, [S3_TARGET], s3=s3, environ=S3_ENV, envs=("production",))

    assert rows_of(rows) == [("yandex", "production", "upload", "ok")]
    head = s3.head_object(Bucket="bucket", Key="v1/production.jws")
    assert head["ContentType"] == "application/jose+json"
    assert head["CacheControl"] == "public, max-age=300"
    assert s3.get_object(Bucket="bucket", Key="v1/production.jws")["Body"].read() == local


def test_unchanged_when_remote_bytes_equal(signed, s3, respx_mock):
    local = signed.dist_path("production").read_bytes()
    s3.put_object(Bucket="bucket", Key="v1/production.jws", Body=local)

    rows = run(signed, [S3_TARGET], s3=s3, environ=S3_ENV, envs=("production",))

    assert rows_of(rows) == [("yandex", "production", "unchanged", "ok")]
    assert respx_mock.calls.call_count == 0


def test_invalid_remote_object_is_overwritten(signed, s3, respx_mock):
    local = signed.dist_path("production").read_bytes()
    s3.put_object(Bucket="bucket", Key="v1/production.jws", Body=b"garbage")
    respx_mock.get(f"{PUBLIC}production.jws").respond(200, content=local)

    rows = run(signed, [S3_TARGET], s3=s3, environ=S3_ENV, envs=("production",))

    assert rows_of(rows) == [("yandex", "production", "upload", "ok")]


def newer_remote_older_local(repo: Repo) -> bytes:
    """Sign version 2, then re-sign version 1 locally; returns the version 2 bytes."""
    repo.write_manifest("production", {**repo.read_manifest("production"), "version": 2})
    repo.sign("production", ["prod-2026a"])
    newer = repo.dist_path("production").read_bytes()
    repo.dist_path("production").unlink()
    repo.write_manifest("production", {**repo.read_manifest("production"), "version": 1})
    repo.sign("production", ["prod-2026a"])
    repo.sign("staging", ["stage-2026a"])
    repo.commit()
    return newer


def test_rollback_refused_when_remote_is_valid_and_not_older(repo, s3):
    newer = newer_remote_older_local(repo)
    s3.put_object(Bucket="bucket", Key="v1/production.jws", Body=newer)

    rows = run(repo, [S3_TARGET], s3=s3, environ=S3_ENV, envs=("production",))

    assert rows_of(rows) == [
        ("yandex", "production", "rollback_refused", "error: rollback_refused")
    ]
    assert s3.get_object(Bucket="bucket", Key="v1/production.jws")["Body"].read() == newer
    assert publish.failed(rows)


def test_rollback_refused_skips_whole_target(repo, s3):
    s3.put_object(Bucket="bucket", Key="v1/production.jws", Body=newer_remote_older_local(repo))

    rows = run(repo, [S3_TARGET], s3=s3, environ=S3_ENV)

    assert objects(s3) == ["v1/production.jws"]
    assert [r.result for r in rows] == ["error: rollback_refused", "skipped"]


def test_missing_credentials_is_a_target_error(signed, s3, respx_mock):
    rows = run(signed, [S3_TARGET, GIT_TARGET], s3=s3, environ={}, envs=("production",))

    assert [(r.target, r.result) for r in rows] == [
        ("yandex", "error: missing_credentials"),
        ("github", "error: missing_credentials"),
    ]
    assert respx_mock.calls.call_count == 0
    assert objects(s3) == []


def test_other_targets_still_publish_after_a_target_error(signed, s3, respx_mock):
    local = signed.dist_path("production").read_bytes()
    respx_mock.get(f"{PUBLIC}production.jws").respond(200, content=local)

    rows = run(signed, [GIT_TARGET, S3_TARGET], s3=s3, environ=S3_ENV, envs=("production",))

    assert [r.result for r in rows] == ["error: missing_credentials", "ok"]
    assert publish.failed(rows)


def test_unreachable_mirror_is_an_error_when_publishing(signed, respx_mock, git_env):
    respx_mock.get(f"{GIT_PUBLIC}production.jws").mock(side_effect=httpx.ConnectError("down"))
    respx_mock.get(f"{GIT_PUBLIC}staging.jws").respond(
        200, content=signed.dist_path("staging").read_bytes()
    )

    rows = run(signed, [GIT_TARGET], environ=git_env, envs=("production",))

    assert rows_of(rows) == [("github", "production", "upload", "error: unreachable")]


def test_dry_run_without_credentials_reads_over_http_and_never_writes(signed, s3, respx_mock):
    runner = FakeRunner()
    local = signed.dist_path("staging").read_bytes()
    respx_mock.get(f"{PUBLIC}production.jws").respond(404)
    respx_mock.get(f"{PUBLIC}staging.jws").respond(200, content=local)
    respx_mock.get(f"{GIT_PUBLIC}production.jws").respond(404)
    respx_mock.get(f"{GIT_PUBLIC}staging.jws").respond(404)

    rows = run(signed, [S3_TARGET, GIT_TARGET], s3=s3, runner=runner, dry_run=True)

    assert rows_of(rows) == [
        ("yandex", "production", "upload", "planned"),
        ("yandex", "staging", "unchanged", "ok"),
        ("github", "production", "upload", "planned"),
        ("github", "staging", "upload", "planned"),
    ]
    assert objects(s3) == []
    assert runner.calls == []
    assert not publish.failed(rows)


def test_dry_run_with_credentials_reads_via_s3_and_never_writes(signed, s3, respx_mock):
    rows = run(signed, [S3_TARGET], s3=s3, environ=S3_ENV, dry_run=True, envs=("production",))

    assert rows_of(rows) == [("yandex", "production", "upload", "planned")]
    assert objects(s3) == []
    assert respx_mock.calls.call_count == 0


def test_dry_run_unreachable_is_a_warning(signed, respx_mock):
    respx_mock.get(url__startswith=PUBLIC).mock(side_effect=httpx.ConnectError("down"))

    rows = run(signed, [S3_TARGET], dry_run=True)

    assert [r.result for r in rows] == ["warning: unreachable"] * 2
    assert [r.action for r in rows] == ["upload"] * 2
    assert not publish.failed(rows)


def test_post_write_mismatch_is_an_error(signed, s3, respx_mock):
    respx_mock.get(f"{PUBLIC}production.jws").respond(200, content=b"stale")

    rows = run(signed, [S3_TARGET], s3=s3, environ=S3_ENV, envs=("production",))

    assert rows_of(rows) == [("yandex", "production", "upload", "error: verify_failed")]


def test_git_push_publishes_all_envs_without_force(signed, respx_mock, git_env):
    runner = FakeRunner()
    bodies = {env: signed.dist_path(env).read_bytes() for env in BOTH_ENVS}
    state = {env: 0 for env in BOTH_ENVS}
    for env in BOTH_ENVS:

        def respond(request, env=env):
            state[env] += 1
            return httpx.Response(404 if state[env] < 3 else 200, content=bodies[env])

        respx_mock.get(f"{GIT_PUBLIC}{env}.jws").mock(side_effect=respond)
    sleeps: list[float] = []

    rows = run(signed, [GIT_TARGET], runner=runner, sleeps=sleeps, environ=git_env)

    assert [r.result for r in rows] == ["ok", "ok"]
    ((command, env, cwd),) = runner.calls
    assert command == ["git", "push", "git@example.com:o/r.git", "HEAD:refs/heads/master"]
    assert runner.ssh[0] == "ssh"
    assert runner.ssh[-8:] == [
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"UserKnownHostsFile={signed.root / 'known_hosts'}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "BatchMode=yes",
    ]
    assert runner.key_mode == 0o600
    assert runner.key_content == DEPLOY_KEY + b"\n"
    assert not Path(runner.ssh[runner.ssh.index("-i") + 1]).exists()
    assert cwd == signed.root
    assert sleeps == [30, 30]


def test_git_stale_cache_is_a_warning(signed, respx_mock, git_env):
    respx_mock.get(url__startswith=GIT_PUBLIC).respond(404)
    sleeps: list[float] = []

    rows = run(signed, [GIT_TARGET], sleeps=sleeps, environ=git_env)

    assert [r.result for r in rows] == ["warning: stale_cache"] * 2
    assert sleeps == [30] * 18
    assert not publish.failed(rows)


def test_git_requires_clean_dist(signed, respx_mock, git_env):
    runner = FakeRunner()
    respx_mock.get(url__startswith=GIT_PUBLIC).respond(404)
    (signed.root / "dist" / "extra.txt").write_text("untracked")

    rows = run(signed, [GIT_TARGET], runner=runner, environ=git_env)

    assert {r.result for r in rows} == {"error: dist_dirty"}
    assert runner.calls == []


def test_git_push_failure_is_an_error(signed, respx_mock, git_env):
    respx_mock.get(url__startswith=GIT_PUBLIC).respond(404)

    rows = run(signed, [GIT_TARGET], runner=FakeRunner(1), environ=git_env)

    assert {r.result for r in rows} == {"error: push_failed: rejected"}
    assert publish.failed(rows)


def test_git_push_failure_reports_last_stderr_line(signed, respx_mock, git_env):
    respx_mock.get(url__startswith=GIT_PUBLIC).respond(404)
    runner = FakeRunner(1, "remote: hint\nHost key verification failed.\n\n")

    rows = run(signed, [GIT_TARGET], runner=runner, environ=git_env)

    assert {r.result for r in rows} == {"error: push_failed: Host key verification failed."}


def test_git_requires_committed_dist(repo, respx_mock, git_env):
    runner = FakeRunner()
    respx_mock.get(url__startswith=GIT_PUBLIC).respond(404)
    repo.commit("without dist")
    repo.sign_all()
    (repo.root / ".git" / "info" / "exclude").write_text("dist/*.jws\n")

    rows = run(repo, [GIT_TARGET], runner=runner, environ=git_env)

    assert {r.result for r in rows} == {"error: dist_not_committed"}
    assert runner.calls == []


def test_unknown_target_is_an_error(signed):
    with pytest.raises(CommandError) as raised:
        run(signed, [S3_TARGET], target_names=("nope",))

    assert raised.value.code == "unknown_target"


def test_s3_write_failure_is_an_error(signed, s3, respx_mock):
    respx_mock.get(url__startswith=PUBLIC).respond(404)

    def refuse(**kwargs):
        raise botocore.exceptions.ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")

    s3.put_object = refuse

    rows = run(signed, [S3_TARGET], s3=s3, environ=S3_ENV, envs=("production",))

    assert rows_of(rows) == [("yandex", "production", "upload", "error: write_failed")]
    assert publish.failed(rows)


def test_main_runs_check_once_before_secrets(signed, tmp_path, monkeypatch, capsys):
    calls = []
    original = check.run
    monkeypatch.setattr(check, "run", lambda **kw: calls.append(kw) or original(**kw))
    signed.age.broken = True

    code = publish.main(
        ["--dry-run", "--secrets", str(tmp_path / "x.age")],
        root=signed.root,
        mirrors_path=write_mirrors(tmp_path / "mirrors.yml"),
        age=signed.age,
        environ={},
    )

    assert code == 1
    assert "secrets_unreadable" in capsys.readouterr().err
    assert len(calls) == 1


def test_main_runs_check_exactly_once(signed, tmp_path, respx_mock, monkeypatch):
    calls = []
    original = check.run
    monkeypatch.setattr(check, "run", lambda **kw: calls.append(kw) or original(**kw))
    respx_mock.get(url__startswith=PUBLIC).respond(404)

    publish.main(
        ["--dry-run"],
        root=signed.root,
        mirrors_path=write_mirrors(tmp_path / "mirrors.yml"),
        environ={},
    )

    assert len(calls) == 1


def test_main_check_failure_stops_before_secrets(repo, tmp_path, capsys):
    repo.age.broken = True

    code = publish.main(
        ["--secrets", str(tmp_path / "x.age")],
        root=repo.root,
        mirrors_path=write_mirrors(tmp_path / "mirrors.yml"),
        age=repo.age,
        environ={},
    )

    assert code == 1
    assert "check_failed" in capsys.readouterr().err


def test_git_unchanged_does_not_push(signed, respx_mock, git_env):
    runner = FakeRunner()
    for env in BOTH_ENVS:
        respx_mock.get(f"{GIT_PUBLIC}{env}.jws").respond(
            200, content=signed.dist_path(env).read_bytes()
        )

    rows = run(signed, [GIT_TARGET], runner=runner, environ=git_env)

    assert [r.action for r in rows] == ["unchanged", "unchanged"]
    assert runner.calls == []


def test_filters_select_target_and_env(signed, s3, respx_mock):
    respx_mock.get(url__startswith=PUBLIC).respond(404)

    rows = run(
        signed,
        [S3_TARGET, GIT_TARGET],
        dry_run=True,
        envs=("staging",),
        target_names=("yandex",),
    )

    assert rows_of(rows) == [("yandex", "staging", "upload", "planned")]


def test_parse_dotenv():
    text = b"# comment\n\nA=1\nexport B='two words'\nC=\"x=y\"\n"

    assert publish.parse_dotenv(text) == {"A": "1", "B": "two words", "C": "x=y"}


def write_mirrors(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "targets": [
                    {
                        "name": "yandex",
                        "type": "s3",
                        "endpoint": "https://s3.example",
                        "region": "ru-central1",
                        "bucket": "bucket",
                        "prefix": "v1/",
                        "public_url": PUBLIC,
                        "credentials": "YANDEX",
                    }
                ]
            }
        )
    )
    return path


def test_main_dry_run_exits_zero_when_mirrors_unreachable(signed, tmp_path, respx_mock, capsys):
    respx_mock.get(url__startswith=PUBLIC).mock(side_effect=httpx.ConnectError("down"))

    code = publish.main(
        ["--dry-run"],
        root=signed.root,
        mirrors_path=write_mirrors(tmp_path / "mirrors.yml"),
        environ={},
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "yandex" in out and "upload" in out and "warning: unreachable" in out


def test_main_secrets_are_decrypted_and_used(signed, tmp_path, s3, respx_mock):
    secrets = tmp_path / "publish.env.age"
    signed.age.secrets[secrets] = (
        b"BOOTSTRAP_YANDEX_ACCESS_KEY_ID=key\nBOOTSTRAP_YANDEX_SECRET_ACCESS_KEY=secret\n"
    )
    for env in BOTH_ENVS:
        respx_mock.get(f"{PUBLIC}{env}.jws").respond(
            200, content=signed.dist_path(env).read_bytes()
        )

    code = publish.main(
        ["--secrets", str(secrets)],
        root=signed.root,
        mirrors_path=write_mirrors(tmp_path / "mirrors.yml"),
        age=signed.age,
        environ={},
        s3_factory=lambda target, key, secret: s3 if (key, secret) == ("key", "secret") else None,
    )

    assert code == 0
    assert objects(s3) == ["v1/production.jws", "v1/staging.jws"]


def test_main_secrets_failure_is_reported(signed, tmp_path, capsys):
    signed.age.broken = True

    code = publish.main(
        ["--secrets", str(tmp_path / "nope.age")],
        root=signed.root,
        mirrors_path=write_mirrors(tmp_path / "mirrors.yml"),
        age=signed.age,
        environ={},
    )

    assert code == 1
    assert "secrets_unreadable" in capsys.readouterr().err


def bump_version(repo: Repo, version: int) -> None:
    for env, kid in (("production", "prod-2026a"), ("staging", "stage-2026a")):
        repo.write_manifest(env, {**repo.read_manifest(env), "version": version})
        repo.sign(env, [kid])
    repo.commit(f"v{version}")


@pytest.fixture
def bare_mirror(signed, tmp_path, respx_mock):
    """A local bare repo holding v1 of both envs, served over HTTP; the work repo is at v2."""
    bare = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    signed.git("push", "-q", str(bare), "HEAD:refs/heads/master")
    bump_version(signed, 2)

    def show(env: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(bare), "show", f"master:dist/{env}.jws"],
            capture_output=True,
            check=True,
        ).stdout

    def head() -> str:
        return subprocess.run(
            ["git", "-C", str(bare), "rev-parse", "master"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    for env in BOTH_ENVS:
        respx_mock.get(f"{GIT_PUBLIC}{env}.jws").mock(
            side_effect=lambda request, env=env: httpx.Response(200, content=show(env))
        )
    target = Target(**{**vars(GIT_TARGET), "push_url": str(bare)})
    return target, head, show


def test_git_env_filter_conflict_blocks_push(signed, bare_mirror, git_env):
    target, head, _ = bare_mirror
    before = head()
    runner = FakeRunner()

    rows = run(signed, [target], runner=runner, environ=git_env, envs=("staging",))

    assert rows_of(rows) == [
        ("github", "production", "upload", "error: env_filter_conflict"),
        ("github", "staging", "upload", "error: env_filter_conflict"),
    ]
    assert runner.calls == []
    assert head() == before
    assert publish.failed(rows)


def test_git_without_env_filter_pushes_all(signed, bare_mirror, git_env):
    target, _, show = bare_mirror

    rows = run(signed, [target], runner=publish._run_command, environ=git_env)

    assert rows_of(rows) == [
        ("github", "production", "upload", "ok"),
        ("github", "staging", "upload", "ok"),
    ]
    assert show("production") == signed.dist_path("production").read_bytes()
    assert show("staging") == signed.dist_path("staging").read_bytes()


def test_git_env_filter_without_other_differences_pushes(signed, respx_mock, git_env):
    runner = FakeRunner()
    respx_mock.get(f"{GIT_PUBLIC}production.jws").respond(404)
    respx_mock.get(f"{GIT_PUBLIC}staging.jws").respond(
        200, content=signed.dist_path("staging").read_bytes()
    )

    rows = run(signed, [GIT_TARGET], runner=runner, environ=git_env, envs=("production",))

    assert rows_of(rows) == [("github", "production", "upload", "warning: stale_cache")]
    assert len(runner.calls) == 1


def test_git_rollback_refused_on_unselected_env_cancels_push(repo, respx_mock, git_env):
    runner = FakeRunner()
    newer = newer_remote_older_local(repo)
    respx_mock.get(f"{GIT_PUBLIC}production.jws").respond(200, content=newer)
    respx_mock.get(f"{GIT_PUBLIC}staging.jws").respond(404)

    rows = run(repo, [GIT_TARGET], runner=runner, environ=git_env, envs=("staging",))

    assert runner.calls == []
    assert "error: rollback_refused" in [r.result for r in rows]
    assert publish.failed(rows)


class RacingS3:
    """Delegates to a real client but lets a concurrent writer act right before put_object."""

    def __init__(self, client, before_put):
        self.client = client
        self.before_put = before_put

    def __getattr__(self, name):
        return getattr(self.client, name)

    def put_object(self, **kwargs):
        self.before_put(kwargs["Key"])
        return self.client.put_object(**kwargs)


def test_s3_write_conflict_when_object_replaced_after_preflight(signed, s3, respx_mock):
    key = "v1/production.jws"
    s3.put_object(Bucket="bucket", Key=key, Body=b"v1-old")
    racing = RacingS3(s3, lambda k: s3.put_object(Bucket="bucket", Key=k, Body=b"v3-other"))

    rows = run(signed, [S3_TARGET], s3=racing, environ=S3_ENV, envs=("production",))

    assert rows_of(rows) == [("yandex", "production", "upload", "error: conflict")]
    assert s3.get_object(Bucket="bucket", Key=key)["Body"].read() == b"v3-other"
    assert publish.failed(rows)


def test_s3_write_conflict_when_new_object_created_after_preflight(signed, s3, respx_mock):
    key = "v1/production.jws"
    racing = RacingS3(s3, lambda k: s3.put_object(Bucket="bucket", Key=k, Body=b"v3-other"))

    rows = run(signed, [S3_TARGET], s3=racing, environ=S3_ENV, envs=("production",))

    assert rows_of(rows) == [("yandex", "production", "upload", "error: conflict")]
    assert s3.get_object(Bucket="bucket", Key=key)["Body"].read() == b"v3-other"


def test_s3_conflict_does_not_stop_other_envs(signed, s3, respx_mock):
    def race(key):
        if key.endswith("production.jws"):
            s3.put_object(Bucket="bucket", Key=key, Body=b"other")

    for env in BOTH_ENVS:
        respx_mock.get(f"{PUBLIC}{env}.jws").respond(
            200, content=signed.dist_path(env).read_bytes()
        )

    rows = run(signed, [S3_TARGET], s3=RacingS3(s3, race), environ=S3_ENV)

    assert [r.result for r in rows] == ["error: conflict", "ok"]


def test_unexpected_exception_becomes_row_error(signed, s3, respx_mock, monkeypatch):
    real = publish.contract.evaluate

    def boom(remote, *, env, **kwargs):
        if env == "production":
            raise ValueError("boom")
        return real(remote, env=env, **kwargs)

    monkeypatch.setattr(publish.contract, "evaluate", boom)
    for env in BOTH_ENVS:
        s3.put_object(Bucket="bucket", Key=f"v1/{env}.jws", Body=b"old")

    rows = run(signed, [S3_TARGET], s3=s3, environ=S3_ENV)

    assert len(rows) == 2
    assert rows[0].result == "error: internal_error: ValueError"
    assert publish.failed(rows)


def test_oversized_mirror_response_is_reported_as_oversized(signed, respx_mock, git_env):
    respx_mock.get(f"{GIT_PUBLIC}production.jws").respond(200, content=b"a" * 70000)
    respx_mock.get(f"{GIT_PUBLIC}staging.jws").respond(
        200, content=signed.dist_path("staging").read_bytes()
    )

    rows = run(signed, [GIT_TARGET], environ=git_env, envs=("production",))

    assert rows_of(rows) == [("github", "production", "upload", "error: oversized")]


def _p(env: str, action: str = "upload", problem: str | None = None) -> publish.Plan:
    return publish.Plan(env, b"local", action, problem)


@pytest.mark.parametrize(
    ("target_type", "plans", "selected", "dry_run", "expected"),
    [
        pytest.param(
            "git",
            [_p("production"), _p("staging")],
            ("production",),
            False,
            [
                ("production", "error", "env_filter_conflict"),
                ("staging", "error", "env_filter_conflict"),
            ],
            id="git_hidden_upload_is_env_filter_conflict",
        ),
        pytest.param(
            "git",
            [_p("production", "unchanged"), _p("staging")],
            ("production",),
            True,
            [("production", "ok", None), ("staging", "error", "env_filter_conflict")],
            id="git_env_filter_conflict_in_dry_run",
        ),
        pytest.param(
            "git",
            [_p("production"), _p("staging", problem="unreachable")],
            ("production",),
            True,
            [("production", "planned", None), ("staging", "planned", "unreachable")],
            id="git_hidden_unreadable_is_no_conflict_in_dry_run",
        ),
        pytest.param(
            "git",
            [_p("production"), _p("staging", "unchanged")],
            ("production",),
            False,
            [("production", "upload", None)],
            id="git_hides_unselected_unchanged",
        ),
        pytest.param(
            "s3",
            [
                _p("production", "rollback_refused"),
                _p("staging"),
            ],
            ("production", "staging"),
            False,
            [("production", "error", "rollback_refused"), ("staging", "skipped", None)],
            id="rollback_refused_skips_other_uploads",
        ),
        pytest.param(
            "git",
            [_p("production"), _p("staging", "rollback_refused")],
            ("production",),
            True,
            [("production", "skipped", None), ("staging", "error", "rollback_refused")],
            id="git_hidden_rollback_refused_wins_over_conflict",
        ),
        pytest.param(
            "s3",
            [_p("production", problem="internal_error: ValueError"), _p("staging", "unchanged")],
            ("production", "staging"),
            True,
            [("production", "error", "internal_error: ValueError"), ("staging", "ok", None)],
            id="internal_error_fails_even_in_dry_run",
        ),
        pytest.param(
            "s3",
            [_p("production", problem="read_failed"), _p("staging")],
            ("production", "staging"),
            True,
            [("production", "planned", "read_failed"), ("staging", "planned", None)],
            id="dry_run_read_problem_is_planned_with_warning",
        ),
        pytest.param(
            "s3",
            [_p("production", problem="read_failed"), _p("staging")],
            ("production", "staging"),
            False,
            [("production", "error", "read_failed"), ("staging", "skipped", None)],
            id="read_problem_fails_without_dry_run",
        ),
        pytest.param(
            "s3",
            [_p("production", "unchanged"), _p("staging")],
            ("production", "staging"),
            False,
            [("production", "ok", None), ("staging", "upload", None)],
            id="clean_plans_upload",
        ),
    ],
)
def test_decide(target_type, plans, selected, dry_run, expected):
    decisions = publish.decide(target_type, plans, selected, dry_run)

    assert [(d.plan.env, d.outcome, d.detail) for d in decisions] == expected


@pytest.mark.parametrize(
    ("outcome", "detail", "result"),
    [
        ("error", "rollback_refused", "error: rollback_refused"),
        ("planned", "read_failed", "warning: read_failed"),
        ("planned", None, "planned"),
        ("skipped", None, "skipped"),
        ("ok", None, "ok"),
    ],
)
def test_decision_result(outcome, detail, result):
    assert publish.Decision(_p("production"), outcome, detail).result == result
