"""`bin/gen-testdata`: regenerate testdata/ with ephemeral keys (private halves are never saved)."""

import argparse
import json
import shutil
import sys
from pathlib import Path

from jwcrypto.jwk import JWK

from bootstrap import jws, keys, manifest
from bootstrap.manifest import ROOT

NOW = "2026-11-02T12:00:00Z"
ISSUED_AT = "2026-11-01T00:00:00Z"
EXPIRES_AT = "2027-04-30T00:00:00Z"
PROD_KID = "prod-9999a"
STAGE_KID = "stage-9999a"
FOREIGN_KID = "prod-9999z"
MAX_SAFE_INTEGER = 2**53 - 1


def base_manifest(env: str, version: int = 1) -> dict:
    mirror_paths = [
        "https://storage.yandexcloud.net/konsierge-bootstrap/v1/{env}.jws",
        "https://konsierge-bootstrap.selstorage.ru/v1/{env}.jws",
        "https://raw.githubusercontent.com/konsierge/bootstrap/master/dist/{env}.jws",
        "https://bootstrap.ai-konsierge.ru/v1/{env}.jws",
    ]
    return {
        "schema": 1,
        "env": env,
        "version": version,
        "issued_at": ISSUED_AT,
        "expires_at": EXPIRES_AT,
        "base_domains": ["konsierge.com", "ai-konsierge.ru"],
        "health_path": "/healthz",
        "mirrors": [path.format(env=env) for path in mirror_paths],
        "policy": {
            "connect_timeout_ms": 3000,
            "probe_interval_s": 600,
            "refresh_interval_s": 21600,
        },
        "min_app_version": {"ios": "0", "android": "0"},
        "notice": {"ru": "", "en": ""},
    }


def _tamper_payload(data: bytes) -> bytes:
    document = json.loads(data)
    payload = document["payload"]
    index = len(payload) // 2
    replacement = "B" if payload[index] != "B" else "C"
    document["payload"] = payload[:index] + replacement + payload[index + 1 :]
    return json.dumps(document, separators=(",", ":")).encode() + b"\n"


def _alg_none(m: dict, key: JWK) -> bytes:
    """A genuine Ed25519 signature under a protected header that declares alg none."""
    payload = jws.b64url_encode(manifest.compact_bytes(m))
    header = json.dumps({"alg": "none", "kid": PROD_KID}, separators=(",", ":"))
    protected = jws.b64url_encode(header.encode())
    signature = jws.b64url_encode(jws.raw_sign(key, f"{protected}.{payload}".encode("ascii")))
    document = {
        "payload": payload,
        "signatures": [{"protected": protected, "signature": signature}],
    }
    return json.dumps(document, separators=(",", ":")).encode("ascii") + b"\n"


def _vector(name, env, expect, *, stored_version=0, now=NOW) -> dict:
    return {
        "name": name,
        "file": f"{name}.jws",
        "env": env,
        "trusted_kids": [PROD_KID if env == "production" else STAGE_KID],
        "stored_version": stored_version,
        "now": now,
        "expect": expect,
    }


def _accepted(version: int, expired: bool = False) -> dict:
    return {"accepted": True, "version": version, "expired": expired}


def _rejected(reason: str) -> dict:
    return {"accepted": False, "reason": reason}


def generate(directory: Path) -> None:
    prod = JWK.generate(kty="OKP", crv="Ed25519")
    stage = JWK.generate(kty="OKP", crv="Ed25519")
    foreign = JWK.generate(kty="OKP", crv="Ed25519")

    def sign(m: dict | bytes, *signers: tuple[str, JWK]) -> bytes:
        payload = m if isinstance(m, bytes) else manifest.compact_bytes(m)
        return jws.sign_general(payload, list(signers))

    prod_signer = (PROD_KID, prod)
    valid = sign(base_manifest("production", 3), prod_signer)

    expired_manifest = base_manifest("production", 3)
    expired_manifest["expires_at"] = "2026-10-01T00:00:00Z"
    bad_mirror = base_manifest("production", 3)
    bad_mirror["mirrors"][0] = bad_mirror["mirrors"][0].replace("https://", "http://", 1)
    unknown_schema = base_manifest("production", 3)
    unknown_schema["schema"] = 2

    cases = [
        (_vector("valid", "production", _accepted(3)), valid),
        (
            _vector("tampered_payload", "production", _rejected("bad_signature")),
            _tamper_payload(valid),
        ),
        (
            _vector("foreign_key", "production", _rejected("bad_signature")),
            sign(base_manifest("production", 3), (PROD_KID, foreign)),
        ),
        (
            _vector("unknown_kid", "production", _rejected("unknown_key")),
            sign(base_manifest("production", 3), (FOREIGN_KID, foreign)),
        ),
        (
            _vector("alg_none", "production", _rejected("unknown_key")),
            _alg_none(base_manifest("production", 3), prod),
        ),
        (
            _vector("rollback", "production", _rejected("rollback"), stored_version=6),
            sign(base_manifest("production", 5), prod_signer),
        ),
        (
            _vector("wrong_env", "production", _rejected("env_mismatch")),
            sign(base_manifest("staging", 3), prod_signer),
        ),
        (
            _vector("staging_key_in_prod", "production", _rejected("unknown_key")),
            sign(base_manifest("staging", 3), (STAGE_KID, stage)),
        ),
        (
            _vector("expired", "production", _accepted(3, expired=True)),
            sign(expired_manifest, prod_signer),
        ),
        (
            _vector("rotation", "production", _accepted(3)),
            sign(base_manifest("production", 3), (FOREIGN_KID, foreign), prod_signer),
        ),
        (
            _vector("bad_mirror_scheme", "production", _rejected("invalid_manifest")),
            sign(bad_mirror, prod_signer),
        ),
        (
            _vector("unknown_schema", "production", _rejected("unknown_schema")),
            sign(unknown_schema, prod_signer),
        ),
        (_vector("malformed", "production", _rejected("malformed")), b"this is not json\n"),
        (
            _vector("version_max", "production", _accepted(MAX_SAFE_INTEGER)),
            sign(base_manifest("production", MAX_SAFE_INTEGER), prod_signer),
        ),
        (
            _vector("version_overflow", "production", _rejected("invalid_manifest")),
            sign(base_manifest("production", MAX_SAFE_INTEGER + 1), prod_signer),
        ),
    ]

    if directory.exists():
        shutil.rmtree(directory)
    (directory / "keys").mkdir(parents=True)
    for kid, key in ((PROD_KID, prod), (STAGE_KID, stage)):
        text = json.dumps(keys.public_jwk(key, kid), indent=2) + "\n"
        (directory / "keys" / f"{kid}.pub.json").write_text(text)
    for vector, data in cases:
        (directory / vector["file"]).write_bytes(data)
    vectors = [vector for vector, _ in cases]
    (directory / "vectors.json").write_text(json.dumps(vectors, indent=2) + "\n")
    (directory / "README.md").write_text(_readme(vectors))


def _readme(vectors: list[dict]) -> str:
    rows = "\n".join(f"| `{v['name']}` | {_describe(v['expect'])} |" for v in vectors)
    return README_TEMPLATE.format(rows=rows, now=NOW)


def _describe(expect: dict) -> str:
    if not expect["accepted"]:
        return f"отказ `{expect['reason']}`"
    return f"принят, `version` {expect['version']}, `expired: {str(expect['expired']).lower()}`"


README_TEMPLATE = """# Тест-векторы bootstrap-манифеста

Файлы сгенерированы `bin/gen-testdata` (не править вручную). Ключи эфемерные: приватные половины
нигде не сохранены, перегенерация меняет все байты. Контракт проверки — `docs/client-contract.md`.

## Состав

- `keys/prod-9999a.pub.json` — доверенный ключ окружения `production`;
  `keys/stage-9999a.pub.json` — доверенный ключ `staging`. Это публичные Ed25519 JWK
  (`{{"kty":"OKP","crv":"Ed25519","x":…,"kid":…}}`).
  Ключ «чужого» подписанта (`foreign`) в `keys/` нарочно отсутствует.
- `<name>.jws` — входные байты клиента, по одному файлу на вектор.
- `vectors.json` — массив объектов:

| Поле | Смысл |
| --- | --- |
| `name`, `file` | имя вектора и файл `.jws` рядом |
| `env` | окружение сборки клиента |
| `trusted_kids` | `kid` из `keys/`, которым клиент доверяет в этом окружении |
| `stored_version` | сохранённая версия клиента (`0`, если нет) |
| `now` | текущее время (RFC 3339), нужно только для флага `expired` |
| `expect` | `{{"accepted":true,"version":N,"expired":bool}}` или `{{"accepted":false,"reason":"<код>"}}` |

## Как использовать

Для каждого вектора выполните проверки из `docs/client-contract.md` над байтами файла с указанными `env`,
доверенными ключами, `stored_version` и `now`; результат обязан совпасть с `expect`. Коды отказа сравниваются
точно, поэтому порядок проверок важен (первый провал определяет код).

Референс: `bin/verify testdata/valid.jws --env production --keys testdata/keys --now {now}`.

## Векторы

| `name` | Ожидание |
| --- | --- |
{rows}

Все векторы, кроме `malformed`, — General JSON Serialization с `payload` из компактного JSON манифеста;
`tampered_payload` отличается от `valid` одним символом `payload`, подпись не пересчитана.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gen-testdata", description=__doc__)
    parser.parse_args(argv)
    generate(ROOT / "testdata")
    print("testdata regenerated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
