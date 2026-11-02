import json

import pytest

from bootstrap import contract, keys
from bootstrap.errors import CommandError
from tests.conftest import NOW


def accepted(repo, env: str):
    trusted = keys.trusted_for_env(keys.load_dir(repo.root / "keys"), env)
    return contract.evaluate(
        repo.dist_path(env).read_bytes(), env=env, trusted_keys=trusted, now=NOW
    )


def sign_error(repo, env: str, kids: list[str]) -> str:
    with pytest.raises(CommandError) as raised:
        repo.sign(env, kids)
    return raised.value.code


def test_sign_writes_verifiable_general_jws_and_rewrites_dates(repo):
    before = repo.read_manifest("production")

    result = repo.sign("production", ["prod-2026a"])

    assert result["version"] == 1
    assert accepted(repo, "production").accepted
    manifest_text = repo.manifest_path("production").read_text()
    assert manifest_text.endswith("\n")
    assert manifest_text == json.dumps(json.loads(manifest_text), indent=2) + "\n"
    after = json.loads(manifest_text)
    assert after["issued_at"] == "2026-11-02T12:00:00Z"
    assert after["expires_at"] == "2027-05-01T12:00:00Z"
    assert list(after) == list(before)
    assert {k: v for k, v in after.items() if "_at" not in k} == {
        k: v for k, v in before.items() if "_at" not in k
    }
    dist = repo.dist_path("production").read_bytes()
    assert dist.endswith(b"}\n")
    assert b" " not in dist
    assert list(json.loads(dist)) == ["payload", "signatures"]


def test_sign_orders_signatures_like_kid_arguments(repo):
    repo.sign("production", ["prod-2026b", "prod-2026a"])

    document = json.loads(repo.dist_path("production").read_bytes())
    assert accepted(repo, "production").valid_kids == ["prod-2026b", "prod-2026a"]
    assert len(document["signatures"]) == 2


def test_sign_rejects_unchanged_version_and_keeps_dist(repo):
    repo.sign("production", ["prod-2026a"])
    signed = repo.dist_path("production").read_bytes()

    assert sign_error(repo, "production", ["prod-2026a"]) == "version_not_increased"

    assert repo.dist_path("production").read_bytes() == signed


def test_sign_accepts_bumped_version(repo):
    repo.sign("production", ["prod-2026a"])
    manifest = repo.read_manifest("production")
    manifest["version"] = 2
    repo.write_manifest("production", manifest)

    assert repo.sign("production", ["prod-2026a"])["version"] == 2
    assert accepted(repo, "production").manifest["version"] == 2


def test_sign_rejects_kid_of_other_environment(repo):
    assert sign_error(repo, "production", ["stage-2026a"]) == "kid_env_mismatch"
    assert not repo.dist_path("production").exists()


def test_sign_rejects_malformed_kid(repo):
    assert sign_error(repo, "production", ["prod-26"]) == "invalid_kid"


def test_sign_rejects_kid_without_public_key_file(repo):
    repo.add_key("prod-2026c", public=False)

    assert sign_error(repo, "production", ["prod-2026c"]) == "unknown_key"
    assert not repo.dist_path("production").exists()


def test_sign_rejects_repeated_kid(repo):
    assert sign_error(repo, "production", ["prod-2026a", "prod-2026a"]) == "duplicate_kid"


def test_sign_rejects_private_key_not_matching_public_file(repo):
    stranger = repo.add_key("prod-2026c")
    repo.write_public("prod-2026b", keys.public_jwk(stranger, "prod-2026b"))

    assert sign_error(repo, "production", ["prod-2026b"]) == "key_mismatch"
    assert not repo.dist_path("production").exists()


def test_sign_reports_missing_private_key_file(repo):
    keys.private_key_path("prod-2026a", repo.key_dir).unlink()

    assert sign_error(repo, "production", ["prod-2026a"]) == "private_key_missing"


def test_sign_reports_age_failure(repo):
    repo.age.broken = True

    assert sign_error(repo, "production", ["prod-2026a"]) == "age_failed"
    assert not repo.dist_path("production").exists()


def test_sign_rejects_manifest_for_other_environment(repo):
    manifest = repo.read_manifest("production")
    manifest["env"] = "staging"
    repo.write_manifest("production", manifest)

    assert sign_error(repo, "production", ["prod-2026a"]) == "env_mismatch"


def test_sign_rejects_schema_violation(repo):
    manifest = repo.read_manifest("production")
    manifest["mirrors"] = ["http://insecure.example/production.jws"]
    repo.write_manifest("production", manifest)

    assert sign_error(repo, "production", ["prod-2026a"]) == "invalid_manifest"
    assert not repo.dist_path("production").exists()


def test_sign_rejects_oversized_file_and_keeps_dist(repo):
    repo.sign("production", ["prod-2026a"])
    signed = repo.dist_path("production").read_bytes()
    manifest = repo.read_manifest("production")
    manifest["version"] = 2
    manifest["base_domains"] += [f"host{i:03d}.example.com" for i in range(120)]
    repo.write_manifest("production", manifest)

    assert sign_error(repo, "production", ["prod-2026a"]) == "too_large"

    assert repo.dist_path("production").read_bytes() == signed


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(version=3.0),
        lambda m: m["policy"].update(connect_timeout_ms=3000.0),
    ],
)
def test_sign_rejects_float_integers(repo, mutate):
    manifest = repo.read_manifest("production")
    mutate(manifest)
    repo.write_manifest("production", manifest)

    assert sign_error(repo, "production", ["prod-2026a"]) == "invalid_manifest"
    assert not repo.dist_path("production").exists()


def test_sign_verify_failure_keeps_dist(repo, monkeypatch):
    repo.sign("production", ["prod-2026a"])
    signed = repo.dist_path("production").read_bytes()
    manifest = repo.read_manifest("production")
    manifest["version"] = 2
    repo.write_manifest("production", manifest)
    monkeypatch.setattr(
        contract, "evaluate", lambda *a, **k: contract.Result(False, "bad_signature")
    )

    assert sign_error(repo, "production", ["prod-2026a"]) == "verify_failed"

    assert repo.dist_path("production").read_bytes() == signed


def test_sign_reports_key_file_without_kid(repo):
    (repo.root / "keys" / "prod-2026c.pub.json").write_text('{"kty": "OKP"}')

    assert sign_error(repo, "production", ["prod-2026a"]) == "bad_key"
    assert not repo.dist_path("production").exists()


def test_sign_reports_unreadable_dist(repo):
    repo.dist_path("production").write_text("garbage")

    assert sign_error(repo, "production", ["prod-2026a"]) == "dist_unreadable"
    assert repo.dist_path("production").read_text() == "garbage"
