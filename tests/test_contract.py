import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jwcrypto.jwk import JWK

from bootstrap import contract, gen_testdata, jws, keys, manifest

TESTDATA = Path(__file__).parent.parent / "testdata"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def load_vectors(directory: Path) -> list[dict]:
    return json.loads((directory / "vectors.json").read_text())


def run_vector(directory: Path, vector: dict) -> contract.Result:
    trusted = keys.trusted_for_env(keys.load_dir(directory / "keys"), vector["env"])
    assert sorted(trusted) == sorted(vector["trusted_kids"])
    return contract.evaluate(
        (directory / vector["file"]).read_bytes(),
        env=vector["env"],
        trusted_keys=trusted,
        stored_version=vector["stored_version"],
        now=datetime.fromisoformat(vector["now"]),
    )


def assert_matches(result: contract.Result, expect: dict) -> None:
    assert result.accepted == expect["accepted"]
    if expect["accepted"]:
        assert result.manifest["version"] == expect["version"]
        assert result.expired == expect["expired"]
    else:
        assert result.reason == expect["reason"]


def test_vector_names_cover_spec():
    names = {v["name"] for v in load_vectors(TESTDATA)}
    assert names == {
        "valid",
        "tampered_payload",
        "foreign_key",
        "unknown_kid",
        "alg_none",
        "rollback",
        "wrong_env",
        "staging_key_in_prod",
        "expired",
        "rotation",
        "bad_mirror_scheme",
        "unknown_schema",
        "malformed",
        "version_max",
        "version_overflow",
    }


@pytest.mark.parametrize("vector", load_vectors(TESTDATA), ids=lambda v: v["name"])
def test_committed_vector(vector):
    assert_matches(run_vector(TESTDATA, vector), vector["expect"])


def test_regenerated_vectors_pass(tmp_path):
    gen_testdata.generate(tmp_path)
    for vector in load_vectors(tmp_path):
        assert_matches(run_vector(tmp_path, vector), vector["expect"])


def test_generated_files_are_general_form_compact(tmp_path):
    gen_testdata.generate(tmp_path)
    raw = (tmp_path / "valid.jws").read_bytes()
    assert raw.endswith(b"}\n")
    doc = json.loads(raw)
    assert set(doc) == {"payload", "signatures"}
    assert list(doc["signatures"][0]) == ["protected", "signature"]
    assert raw == json.dumps(doc, separators=(",", ":")).encode() + b"\n"
    header = jws.b64url_decode(doc["signatures"][0]["protected"])
    assert header == b'{"alg":"EdDSA","kid":"prod-9999a"}'


# Single-case contract behaviour on hand-built input.


@pytest.fixture
def signer():
    key = JWK.generate(kty="OKP", crv="Ed25519")
    public = json.loads(key.export_public())
    public["kid"] = "prod-0001a"
    return key, {"prod-0001a": public}


def doc_of(manifest_obj: dict, key: JWK, kid: str = "prod-0001a") -> bytes:
    payload = manifest.compact_bytes(manifest_obj)
    return jws.sign_general(payload, [(kid, key)])


def good_manifest() -> dict:
    return gen_testdata.base_manifest("production", version=3)


def evaluate(data: bytes, trusted: dict, **kw) -> contract.Result:
    kw.setdefault("stored_version", 0)
    return contract.evaluate(data, env="production", trusted_keys=trusted, now=NOW, **kw)


def test_accepts_valid(signer):
    key, trusted = signer
    result = evaluate(doc_of(good_manifest(), key), trusted)
    assert result.accepted
    assert result.valid_kids == ["prod-0001a"]
    assert result.expired is False


def test_equal_version_is_not_rollback(signer):
    key, trusted = signer
    assert evaluate(doc_of(good_manifest(), key), trusted, stored_version=3).accepted


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"[]",
        b'{"payload":"e30","signatures":[]}',
        b'{"payload":"e30","protected":"e30","signature":"AA"}',  # flattened form
        b'{"payload":1,"signatures":[{"protected":"e30","signature":"AA"}]}',
        b'{"payload":"e30","signatures":[{"protected":"e30"}]}',
        b'{"payload":"e30","signatures":[{"protected":"e30","signature":"A*"}]}',
        b'{"payload":"e30=","signatures":[{"protected":"e30","signature":"AA"}]}',
    ],
)
def test_malformed_envelopes(data, signer):
    assert evaluate(data, signer[1]).reason == "malformed"


def resign_with_header(header: dict, key: JWK) -> bytes:
    payload = jws.b64url_encode(manifest.compact_bytes(good_manifest()))
    protected = jws.b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    signature = jws.raw_sign(key, f"{protected}.{payload}".encode("ascii"))
    sig = {"protected": protected, "signature": jws.b64url_encode(signature)}
    return json.dumps({"payload": payload, "signatures": [sig]}).encode()


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "EdDSA", "kid": "prod-0001a", "crit": ["x"], "x": 1},
        {"alg": "none", "kid": "prod-0001a"},
        {"alg": "EdDSA"},
    ],
)
def test_ineligible_signature_is_skipped(header, signer):
    key, trusted = signer
    assert evaluate(resign_with_header(header, key), trusted).reason == "unknown_key"


def test_extra_header_fields_are_ignored(signer):
    key, trusted = signer
    header = {"alg": "EdDSA", "kid": "prod-0001a", "typ": "JOSE"}
    assert evaluate(resign_with_header(header, key), trusted).accepted


def test_payload_not_utf8_json_object_is_malformed(signer):
    key, trusted = signer
    for payload in (b"\xff\xfe", b"[1]", b'{"schema":"1"}', b'{"schema":true}'):
        data = jws.sign_general(payload, [("prod-0001a", key)])
        assert evaluate(data, trusted).reason == "malformed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.pop("mirrors"),
        lambda m: m.update(mirrors=[]),
        lambda m: m.update(version=0),
        lambda m: m.update(version=3.0),
        lambda m: m["policy"].update(connect_timeout_ms=3000.0),
        lambda m: m.update(base_domains=["Example.com"]),
        lambda m: m["policy"].pop("probe_interval_s"),
        lambda m: m.update(expires_at="2026-13-45T00:00:00Z"),
    ],
)
def test_invalid_manifest(mutate, signer):
    key, trusted = signer
    m = good_manifest()
    mutate(m)
    assert evaluate(doc_of(m, key), trusted).reason == "invalid_manifest"


def test_unknown_fields_are_tolerated(signer):
    key, trusted = signer
    m = good_manifest()
    m["future"] = {"a": 1}
    m["policy"]["future"] = 1
    assert evaluate(doc_of(m, key), trusted).accepted


def test_unknown_schema_precedes_invalid_manifest(signer):
    key, trusted = signer
    m = good_manifest()
    m["schema"] = 2
    del m["mirrors"]
    assert evaluate(doc_of(m, key), trusted).reason == "unknown_schema"


def test_expired_is_soft(signer):
    key, trusted = signer
    m = good_manifest()
    m["expires_at"] = "2026-11-02T12:00:00Z"
    result = evaluate(doc_of(m, key), trusted)
    assert result.accepted
    assert result.expired is True


def test_one_valid_signature_is_enough(signer):
    key, trusted = signer
    other = JWK.generate(kty="OKP", crv="Ed25519")
    payload = manifest.compact_bytes(good_manifest())
    data = jws.sign_general(payload, [("prod-0001a", other), ("prod-0001a", key)])
    assert evaluate(data, trusted).valid_kids == ["prod-0001a"]


def test_result_dict_shapes(signer):
    key, trusted = signer
    ok = evaluate(doc_of(good_manifest(), key), trusted).as_dict()
    assert list(ok) == ["accepted", "env", "version", "valid_kids", "expires_at", "expired"]
    assert evaluate(b"x", trusted).as_dict() == {"accepted": False, "reason": "malformed"}


def test_deeply_nested_json_is_malformed(signer):
    _, trusted = signer
    data = b"[" * 10000 + b"0" + b"]" * 10000
    assert evaluate(data, trusted).reason == "malformed"


def test_impossible_calendar_date_is_invalid_manifest(signer):
    key, trusted = signer
    m = good_manifest()
    m["issued_at"] = "2026-02-30T12:00:00Z"
    assert evaluate(doc_of(m, key), trusted).reason == "invalid_manifest"
    assert "issued_at" in manifest.schema_errors(m)[0]
