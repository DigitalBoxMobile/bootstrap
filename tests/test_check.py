import json

import pytest
from jwcrypto.jwk import JWK

from bootstrap import check, jws, keys
from bootstrap.errors import CommandError
from tests.conftest import NOW


def error_codes(repo, base: str | None = None) -> list[str]:
    results = check.run(root=repo.root, base=base)
    return sorted(r.error.code for r in results if r.error)


@pytest.fixture
def signed(repo):
    repo.sign_all()
    return repo


def bump_and_sign(repo, env: str, kid: str, version: int | None = None, now=NOW) -> None:
    manifest = repo.read_manifest(env)
    manifest["version"] = version or manifest["version"] + 1
    repo.write_manifest(env, manifest)
    repo.sign(env, [kid], now=now)


def test_check_passes_on_signed_repository(signed):
    results = check.run(root=signed.root)

    assert results
    assert all(r.error is None for r in results)


def test_check_reports_missing_dist_files(repo):
    assert error_codes(repo) == ["dist_missing", "dist_missing"]


def test_check_detects_manifest_edited_after_signing(signed):
    manifest = signed.read_manifest("production")
    manifest["health_path"] = "/other"
    signed.write_manifest("production", manifest)

    assert error_codes(signed) == ["payload_mismatch"]


def test_check_detects_dist_with_foreign_signature(signed):
    manifest = signed.read_manifest("production")
    stranger = JWK.generate(kty="OKP", crv="Ed25519")
    forged = jws.sign_general(json.dumps(manifest).encode(), [("prod-2026a", stranger)])
    signed.dist_path("production").write_bytes(forged)

    assert error_codes(signed) == ["bad_signature"]


def test_check_detects_unknown_signing_key(signed):
    bump_and_sign(signed, "production", "prod-2026b")
    (signed.root / "keys" / "prod-2026b.pub.json").unlink()

    assert error_codes(signed) == ["unknown_key"]


def test_check_rejects_private_scalar_in_keys_directory(signed):
    private = json.loads(signed.privates["prod-2026b"].export_private())
    private["kid"] = "prod-2026b"
    signed.write_public("prod-2026b", {**private, "use": "sig", "alg": "EdDSA"})

    assert error_codes(signed) == ["bad_key"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda jwk: jwk.pop("use"),
        lambda jwk: jwk.update(use="enc"),
        lambda jwk: jwk.pop("alg"),
        lambda jwk: jwk.update(alg="RS256"),
        lambda jwk: jwk.update(k="c2VjcmV0"),
        lambda jwk: jwk.update(p="c2VjcmV0"),
    ],
)
def test_check_rejects_wrong_jwk_shape(signed, mutate):
    jwk = keys.public_jwk(signed.privates["prod-2026b"], "prod-2026b")
    mutate(jwk)
    signed.write_public("prod-2026b", jwk)

    assert error_codes(signed) == ["bad_key"]


def test_check_rejects_kid_that_differs_from_filename(signed):
    jwk = keys.public_jwk(signed.privates["prod-2026b"], "prod-2026a")
    signed.write_public("prod-2026b", jwk)

    assert "bad_key" in error_codes(signed)


def test_check_rejects_extra_file_in_keys_directory(signed):
    (signed.root / "keys" / "notes.txt").write_text("hello")

    assert error_codes(signed) == ["unexpected_file"]


def test_check_allows_gitkeep_in_keys_directory(signed):
    (signed.root / "keys" / ".gitkeep").touch()

    assert error_codes(signed) == []


def test_check_rejects_manifest_for_other_environment(signed):
    manifest = signed.read_manifest("staging")
    manifest["env"] = "production"
    signed.write_manifest("staging", manifest)

    assert "env_mismatch" in error_codes(signed)


def test_check_rejects_expiry_before_issue(signed):
    manifest = signed.read_manifest("staging")
    manifest["expires_at"] = "2020-01-01T00:00:00Z"
    signed.write_manifest("staging", manifest)

    assert "expiry_order" in error_codes(signed)


def test_check_rejects_schema_violation(signed):
    manifest = signed.read_manifest("staging")
    del manifest["policy"]
    signed.write_manifest("staging", manifest)

    assert "invalid_manifest" in error_codes(signed)


def test_check_rejects_oversized_dist(signed):
    padded = signed.dist_path("staging").read_bytes() + b" " * 4000
    signed.dist_path("staging").write_bytes(padded)

    assert "too_large" in error_codes(signed)


def test_check_accepts_expired_manifest(signed):
    bump_and_sign(signed, "staging", "stage-2026a", now=NOW.replace(year=2000))

    assert error_codes(signed) == []


# --base


def test_base_accepts_version_increase(signed):
    base = signed.commit("v1")
    bump_and_sign(signed, "production", "prod-2026a")

    assert error_codes(signed, base) == []


def test_base_rejects_changed_dist_without_version_increase(signed):
    base = signed.commit("v1")
    # Re-signing the same manifest with another key changes the bytes, not the version.
    signed.dist_path("production").write_bytes(
        jws.sign_general(
            json.dumps(signed.read_manifest("production"), separators=(",", ":")).encode(),
            [("prod-2026b", signed.privates["prod-2026b"])],
        )
    )

    assert error_codes(signed, base) == ["version_not_increased"]


def test_base_rejects_version_decrease(signed):
    bump_and_sign(signed, "production", "prod-2026a", version=5)
    base = signed.commit("v5")
    manifest = signed.read_manifest("production")
    manifest["version"] = 3
    signed.write_manifest("production", manifest)
    signed.dist_path("production").unlink()
    signed.sign("production", ["prod-2026a"])

    assert error_codes(signed, base) == ["version_not_increased"]


def test_base_ignores_untouched_dist(signed):
    base = signed.commit("v1")
    bump_and_sign(signed, "staging", "stage-2026a")

    assert error_codes(signed, base) == []


def test_base_accepts_new_dist_missing_in_base(repo):
    base = repo.commit("empty")
    repo.sign_all()

    assert error_codes(repo, base) == []


def test_base_rejects_deleted_dist(signed):
    base = signed.commit("v1")
    signed.dist_path("staging").unlink()

    codes = error_codes(signed, base)

    assert "dist_deleted" in codes


def test_base_with_unknown_ref_is_an_error(signed):
    with pytest.raises(CommandError) as raised:
        check.run(root=signed.root, base="no-such-ref")
    assert raised.value.code == "bad_base"


def test_check_reports_unreadable_dist_against_base(repo):
    base = repo.commit("empty")
    repo.dist_path("staging").write_text("garbage")

    assert "dist_unreadable" in error_codes(repo, base)
