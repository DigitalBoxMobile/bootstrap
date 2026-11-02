import subprocess
from pathlib import Path

import boto3
import httpx
import pytest
import yaml
from moto import mock_aws

from bootstrap import publish
from bootstrap.errors import CommandError
from bootstrap.mirrors import Target
from tests.conftest import Repo

PUBLIC = "https://pub.example/v1/"
GIT_PUBLIC = "https://raw.example/dist/"
S3_ENV = {
    "BOOTSTRAP_YANDEX_ACCESS_KEY_ID": "key",
    "BOOTSTRAP_YANDEX_SECRET_ACCESS_KEY": "secret",
}
GIT_ENV = {"BOOTSTRAP_GITHUB_DEPLOY_KEY": "/keys/deploy"}
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
)


class FakeRunner:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict, Path]] = []

    def __call__(self, command, *, env, cwd):
        self.calls.append((command, env, cwd))
        return subprocess.CompletedProcess(command, self.returncode, "", "rejected")


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


def test_unreachable_mirror_is_an_error_when_publishing(signed, respx_mock):
    respx_mock.get(f"{GIT_PUBLIC}production.jws").mock(side_effect=httpx.ConnectError("down"))

    rows = run(signed, [GIT_TARGET], environ=GIT_ENV, envs=("production",))

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


def test_git_push_publishes_all_envs_without_force(signed, respx_mock):
    runner = FakeRunner()
    bodies = {env: signed.dist_path(env).read_bytes() for env in BOTH_ENVS}
    state = {env: 0 for env in BOTH_ENVS}
    for env in BOTH_ENVS:

        def respond(request, env=env):
            state[env] += 1
            return httpx.Response(404 if state[env] < 3 else 200, content=bodies[env])

        respx_mock.get(f"{GIT_PUBLIC}{env}.jws").mock(side_effect=respond)
    sleeps: list[float] = []

    rows = run(signed, [GIT_TARGET], runner=runner, sleeps=sleeps, environ=GIT_ENV)

    assert [r.result for r in rows] == ["ok", "ok"]
    ((command, env, cwd),) = runner.calls
    assert command == ["git", "push", "git@example.com:o/r.git", "HEAD:refs/heads/master"]
    assert env["GIT_SSH_COMMAND"] == "ssh -i /keys/deploy -o IdentitiesOnly=yes"
    assert cwd == signed.root
    assert sleeps == [30, 30]


def test_git_stale_cache_is_a_warning(signed, respx_mock):
    respx_mock.get(url__startswith=GIT_PUBLIC).respond(404)
    sleeps: list[float] = []

    rows = run(signed, [GIT_TARGET], sleeps=sleeps, environ=GIT_ENV, envs=("production",))

    assert rows_of(rows) == [("github", "production", "upload", "warning: stale_cache")]
    assert sleeps == [30] * 9
    assert not publish.failed(rows)


def test_git_requires_clean_dist(signed, respx_mock):
    runner = FakeRunner()
    respx_mock.get(url__startswith=GIT_PUBLIC).respond(404)
    (signed.root / "dist" / "extra.txt").write_text("untracked")

    rows = run(signed, [GIT_TARGET], runner=runner, environ=GIT_ENV)

    assert {r.result for r in rows} == {"error: dist_dirty"}
    assert runner.calls == []


def test_git_push_failure_is_an_error(signed, respx_mock):
    respx_mock.get(url__startswith=GIT_PUBLIC).respond(404)

    rows = run(signed, [GIT_TARGET], runner=FakeRunner(1), environ=GIT_ENV)

    assert {r.result for r in rows} == {"error: push_failed"}


def test_git_unchanged_does_not_push(signed, respx_mock):
    runner = FakeRunner()
    for env in BOTH_ENVS:
        respx_mock.get(f"{GIT_PUBLIC}{env}.jws").respond(
            200, content=signed.dist_path(env).read_bytes()
        )

    rows = run(signed, [GIT_TARGET], runner=runner, environ=GIT_ENV)

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


def test_check_failure_stops_publishing(repo, s3):
    with pytest.raises(CommandError) as raised:
        run(repo, [S3_TARGET], s3=s3, environ=S3_ENV)

    assert raised.value.code == "check_failed"
    assert objects(s3) == []


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
