# Hardening публикации и контракта — план

Статус: `ready for implementation`. Спека — `spec.md`.

## Подход

Четыре независимых по файлам среза, тесты сначала. Манифесты и `dist/` не меняются; публикация на зеркала не нужна.

## Срезы

### 1. Устойчивость разбора и даты (F3, F5)

Файлы: `bootstrap/http.py`, `bootstrap/contract.py`, `bootstrap/manifest.py`, `bootstrap/check.py`, `bootstrap/sign.py`,
`bootstrap/check_mirrors.py`, тесты `tests/test_http.py`, `tests/test_contract.py`, `tests/test_check.py`, `tests/test_sign.py`,
`tests/test_check_mirrors.py`.

Готово, когда: потоковое чтение с лимитом 65536 (`oversized`); `RecursionError` → `malformed`; изоляция ошибок по URL
в `check_mirrors` (`internal_error`); календарная проверка дат в `schema_errors`, `check`/`sign` без traceback.

### 2. Публикация: git и условная запись (F1, F2)

Файлы: `bootstrap/publish.py`, `tests/test_publish.py`. Опирается на `http.get` из среза 1 (сигнатура не меняется).

Готово, когда: git-цель проверяет все env и отказывает `env_filter_conflict` по правилам спеки (тест на bare-репо);
S3 пишет с `IfMatch`/`IfNoneMatch="*"`, `412` → `conflict` (тест с подменой объекта между preflight и записью; если `moto`
не поддерживает условия — подменить клиент S3 своим фейком на этом тесте); preflight в `publish` изолирует ошибки по цели.

### 3. Контракт: целые и векторы (уточнение 2)

Файлы: `schema/manifest.v1.json`, `bootstrap/gen_testdata.py`, `testdata/**` (регенерация `bin/gen-testdata`),
`tests/test_contract.py`, `tests/test_vectors_raw.py` при необходимости.

Готово, когда: `maximum: 9007199254740991` у всех целых полей; векторы `version_max` (принят) и `version_overflow`
(`invalid_manifest`); все векторы проходят.

### 4. Документы (уточнения 1, 3, 4; коды F1–F3)

Файлы: `docs/client-contract.md`, `README.md`, `testdata/README.md` (если затронут срезом 3 — уже регенерирован).

Готово, когда: разделы «Выбор домена», «Получение и кеш» и новый «Обязательные сценарии клиента» содержат пп. 1, 3, 4
спеки; утверждение о совпадении схемы и таблицы уточнено (календарные даты); README описывает `conflict`,
`env_filter_conflict`, `oversized`.

Порядок: 1 → 2 (зависимость по `http.py`), 3 и 4 после 1 (файлы не пересекаются с 2 и между собой; 4 после 3 из-за векторов).

## Проверка

```
uv run pytest
uv run ruff check
uv run ruff format --check
bin/check
bin/check-mirrors
```

`bin/check-mirrors` — живая проверка; ожидается выход `0` (зеркала на `version 3`).

## Полномочия и передача

- **Реализация разрешена:** да — «давай, согласен» пользователя 2026-11-02 на hardening-пакет.
- **Task branch:** `KON-11445`
- **Delivery target:** локальный merge `KON-11445` → `master`. Push и публикация на зеркала не нужны (`dist/` не меняется).
- **Review tier:** `fable`
- **Gate:** `uv run pytest`; `uv run ruff check`; `uv run ruff format --check`; `bin/check`; `bin/check-mirrors`.
- **Excluded actions:** изменения `manifests/`, `dist/`, `keys/`; запись на зеркала; remote и push в GitLab.
- **Next session:** `/deploy docs/requests/bootstrap-hardening`
