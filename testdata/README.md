# Тест-векторы bootstrap-манифеста

Файлы сгенерированы `bin/gen-testdata` (не править вручную). Ключи эфемерные: приватные половины
нигде не сохранены, перегенерация меняет все байты. Контракт проверки — `docs/client-contract.md`.

## Состав

- `keys/prod-9999a.pub.json` — доверенный ключ окружения `production`;
  `keys/stage-9999a.pub.json` — доверенный ключ `staging`. Это публичные Ed25519 JWK
  (`{"kty":"OKP","crv":"Ed25519","x":…,"kid":…}`).
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
| `expect` | `{"accepted":true,"version":N,"expired":bool}` или `{"accepted":false,"reason":"<код>"}` |

## Как использовать

Для каждого вектора выполните проверки из `docs/client-contract.md` над байтами файла с указанными `env`,
доверенными ключами, `stored_version` и `now`; результат обязан совпасть с `expect`. Коды отказа сравниваются
точно, поэтому порядок проверок важен (первый провал определяет код).

Референс: `bin/verify testdata/valid.jws --env production --keys testdata/keys --now 2026-11-02T12:00:00Z`.

## Векторы

| `name` | Ожидание |
| --- | --- |
| `valid` | принят, `version` 3, `expired: false` |
| `tampered_payload` | отказ `bad_signature` |
| `foreign_key` | отказ `bad_signature` |
| `unknown_kid` | отказ `unknown_key` |
| `rollback` | отказ `rollback` |
| `wrong_env` | отказ `env_mismatch` |
| `staging_key_in_prod` | отказ `unknown_key` |
| `expired` | принят, `version` 3, `expired: true` |
| `rotation` | принят, `version` 3, `expired: false` |
| `bad_mirror_scheme` | отказ `invalid_manifest` |
| `unknown_schema` | отказ `unknown_schema` |
| `malformed` | отказ `malformed` |

Все векторы, кроме `malformed`, — General JSON Serialization с `payload` из компактного JSON манифеста;
`tampered_payload` отличается от `valid` одним символом `payload`, подпись не пересчитана.
