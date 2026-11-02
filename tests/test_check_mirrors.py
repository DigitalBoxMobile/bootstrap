import json
from datetime import datetime, timedelta

import httpx
import pytest

from bootstrap import check_mirrors
from tests.conftest import NOW, Repo

PROD_URLS = [
    "https://storage.yandexcloud.net/konsierge-bootstrap/v1/production.jws",
    "https://konsierge-bootstrap.selstorage.ru/v1/production.jws",
    "https://raw.githubusercontent.com/konsierge/bootstrap/master/dist/production.jws",
    "https://bootstrap.ai-konsierge.ru/v1/production.jws",
]


@pytest.fixture
def signed(repo: Repo) -> Repo:
    repo.sign_all()
    return repo


def serve_reference(repo: Repo, respx_mock, env: str = "production") -> bytes:
    data = repo.dist_path(env).read_bytes()
    manifest = repo.read_manifest(env)
    for url in manifest["mirrors"]:
        respx_mock.get(url).respond(200, content=data)
    return data


def problems_by_url(rows) -> dict[str, list[str]]:
    return {r.url: r.problems for r in rows}


def run(repo: Repo, **kwargs):
    return check_mirrors.run(root=repo.root, envs=("production",), now=NOW, **kwargs)


def test_all_mirrors_match(signed, respx_mock):
    serve_reference(signed, respx_mock)

    rows = run(signed)

    assert [r.url for r in rows] == PROD_URLS
    assert all(r.ok for r in rows)


def test_non_200_is_http_error(signed, respx_mock):
    serve_reference(signed, respx_mock)
    respx_mock.get(PROD_URLS[0]).respond(404)

    assert problems_by_url(run(signed))[PROD_URLS[0]] == ["http_error"]


def test_network_failure_is_unreachable(signed, respx_mock):
    serve_reference(signed, respx_mock)
    respx_mock.get(PROD_URLS[1]).mock(side_effect=httpx.ConnectTimeout("slow"))

    assert problems_by_url(run(signed))[PROD_URLS[1]] == ["unreachable"]


def test_contract_failure_reports_reason_code(signed, respx_mock):
    serve_reference(signed, respx_mock)
    respx_mock.get(PROD_URLS[2]).respond(200, content=b"not json")

    assert problems_by_url(run(signed))[PROD_URLS[2]] == ["malformed"]


def test_older_version_is_version_behind(signed, respx_mock):
    old = signed.dist_path("production").read_bytes()
    manifest = signed.read_manifest("production")
    manifest["version"] += 1
    signed.write_manifest("production", manifest)
    signed.sign("production", ["prod-2026a"])
    serve_reference(signed, respx_mock)
    respx_mock.get(PROD_URLS[0]).respond(200, content=old)

    assert problems_by_url(run(signed))[PROD_URLS[0]] == ["version_behind"]


def test_newer_version_is_version_ahead(signed, respx_mock):
    reference = signed.dist_path("production").read_bytes()
    manifest = signed.read_manifest("production")
    manifest["version"] += 1
    signed.write_manifest("production", manifest)
    signed.sign("production", ["prod-2026a"])
    newer = signed.dist_path("production").read_bytes()
    signed.dist_path("production").write_bytes(reference)
    serve_reference(signed, respx_mock)
    respx_mock.get(PROD_URLS[3]).respond(200, content=newer)

    assert problems_by_url(run(signed))[PROD_URLS[3]] == ["version_ahead"]


def test_same_version_other_bytes_is_content_mismatch(signed, respx_mock):
    reference = serve_reference(signed, respx_mock)
    other = json.loads(reference)
    other["signatures"].reverse()
    other["signatures"].append(dict(other["signatures"][0]))
    respx_mock.get(PROD_URLS[0]).respond(200, content=json.dumps(other).encode())

    assert problems_by_url(run(signed))[PROD_URLS[0]] == ["content_mismatch"]


def test_expiring_manifest_flags_every_mirror(signed, respx_mock):
    serve_reference(signed, respx_mock)
    expires = signed.read_manifest("production")["expires_at"]
    close = datetime.fromisoformat(expires.replace("Z", "+00:00")) - timedelta(days=59)

    rows = check_mirrors.run(root=signed.root, envs=("production",), now=close, warn_days=60)

    assert all(r.problems == ["expiring"] for r in rows)


def test_expire_warn_days_zero_ignores_expiry(signed, respx_mock):
    serve_reference(signed, respx_mock)

    assert all(r.ok for r in run(signed, warn_days=0))


def test_missing_dist_is_a_clean_error(repo, capsys):
    status = check_mirrors.main(["--env", "production"], root=repo.root)

    assert status == 1
    assert "dist_missing" in capsys.readouterr().err


def test_main_exit_zero_and_text(signed, respx_mock, capsys):
    serve_reference(signed, respx_mock)

    status = check_mirrors.main(
        ["--env", "production", "--now", "2026-11-02T12:00:00Z"], root=signed.root
    )

    assert status == 0
    assert capsys.readouterr().out.count("ok ") == 4


def test_main_json_format_and_exit_one(signed, respx_mock, capsys):
    serve_reference(signed, respx_mock)
    respx_mock.get(PROD_URLS[0]).respond(500)

    status = check_mirrors.main(
        ["--env", "production", "--format", "json", "--now", "2026-11-02T12:00:00Z"],
        root=signed.root,
    )

    rows = json.loads(capsys.readouterr().out)
    assert status == 1
    assert rows[0] == {
        "env": "production",
        "url": PROD_URLS[0],
        "ok": False,
        "problems": ["http_error"],
    }
    assert [r["ok"] for r in rows] == [False, True, True, True]


def test_env_filter_checks_only_selected_env(signed, respx_mock, capsys):
    serve_reference(signed, respx_mock, "staging")

    status = check_mirrors.main(
        ["--env", "staging", "--format", "json", "--now", "2026-11-02T12:00:00Z"], root=signed.root
    )

    assert status == 0
    assert {r["env"] for r in json.loads(capsys.readouterr().out)} == {"staging"}
