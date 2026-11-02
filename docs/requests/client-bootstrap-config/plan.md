# Bootstrap-манифесты: этапы 0 и 1 — план

Статус: `ready for implementation`. Спека — `spec.md`, архитектура — `design.md`.

## Подход

Python-пакет `bootstrap/` под `uv` с тонкими обёртками в `bin/`. Ядро — модуль контракта (`bootstrap/contract.py`):
разбор JWS General JSON, проверка подписей через `jwcrypto`, схема, коды отказа. На нём стоят `verify`, `sign`, `check`,
`publish`, `check-mirrors` и генератор векторов — одна реализация проверки на всё. Внешние эффекты (`age`, S3, HTTP, `git push`)
изолированы в небольших адаптерах, чтобы тесты подменяли их без сети и без `age`.

## Подготовка

- `brew install age` (нужен владельцу и для ручной проверки `keygen`; тесты `age` не требуют). Установку записать в `verification.md`.
- Репозиторий без коммитов: корневой коммит на `master` с текущими `AGENTS.md`, `README.md`, `docs/requests/client-bootstrap-config/design.md`,
  затем ветка `KON-11445` от него; пакет (`spec.md`, `plan.md`, `BACKLOG.md`) — первый коммит ветки. Делает сессия проектирования.
- Рабочее место — основная копия на ветке `KON-11445` (репозиторий новый, параллельной работы нет).

## Срезы

Срезы последовательны: каждый опирается на модули предыдущего. Все — `implementer` (Sonnet); механических нет.

### 1. Контракт, `bin/verify`, тест-векторы

Файлы: `pyproject.toml`, `uv.lock`, `.python-version`, `bootstrap/__init__.py`, `bootstrap/manifest.py` (загрузка, компактная
сериализация, схема), `bootstrap/contract.py`, `bootstrap/keys.py` (загрузка `keys/`, `kid` → env), `bootstrap/verify.py`,
`bootstrap/gen_testdata.py`, `bin/verify`, `bin/gen-testdata`, `schema/manifest.v1.json`, `testdata/**`, `docs/client-contract.md`,
`tests/test_contract.py`, `tests/test_vectors_raw.py`.

Готово, когда: схема соответствует таблице спеки; все 12 векторов сгенерированы, `vectors.json` заполнен, тесты проходят
каждый вектор через `contract` и через «сырой» Ed25519 (`cryptography`, без `jwcrypto`) для векторов, где решение зависит
от подписи; `bin/verify testdata/valid.jws --env production --keys testdata/keys --now <now вектора>` печатает `accepted: true`;
`docs/client-contract.md` и `testdata/README.md` соответствуют разделу «Контракт клиента».

### 2. `bin/keygen`, `bin/sign`, `bin/check`, манифесты

Файлы: `bootstrap/age.py` (адаптер `age -p`/`age -d` через подпроцесс с наследуемым TTY), `bootstrap/keygen.py`, `bootstrap/sign.py`,
`bootstrap/check.py`, `bin/keygen`, `bin/sign`, `bin/check`, `manifests/production.json`, `manifests/staging.json` (`version: 1`,
даты-заглушки — `bin/sign` их перепишет), `.gitignore`, `keys/.gitkeep`, `dist/.gitkeep`, `tests/test_sign.py`, `tests/test_check.py`.

Готово, когда: тесты `sign` с эфемерным ключом и подменённым адаптером `age` покрывают успех, `version_not_increased`, чужой `kid`,
несовпадение публичной части, превышение размера, неизменность `dist/` при ошибке; тесты `check` на временном git-репозитории
покрывают `payload_mismatch`, `bad_signature`, приватный `d` в `keys/`, лишний файл в `keys/`, `--base` с ростом/без роста версии,
новый и удалённый `dist/`; `bin/keygen` отказывается писать внутрь репозитория (тест без вызова `age`).

### 3. `mirrors.yml`, `bin/publish`

Файлы: `mirrors.yml`, `bootstrap/mirrors.py` (загрузка и проверка конфигурации), `bootstrap/publish.py`, `bootstrap/http.py`
(общий GET с таймаутом, используется и в срезе 4), `bin/publish`, `tests/test_publish.py`.

`mirrors.yml`:

```yaml
targets:
  - name: yandex
    type: s3
    endpoint: https://storage.yandexcloud.net
    region: ru-central1
    bucket: konsierge-bootstrap
    prefix: v1/
    public_url: https://storage.yandexcloud.net/konsierge-bootstrap/v1/
    credentials: YANDEX        # BOOTSTRAP_YANDEX_ACCESS_KEY_ID, BOOTSTRAP_YANDEX_SECRET_ACCESS_KEY
  - name: selectel
    type: s3
    endpoint: https://s3.storage.selcloud.ru
    region: ru-1
    bucket: konsierge-bootstrap
    prefix: v1/
    public_url: https://konsierge-bootstrap.selstorage.ru/v1/
    credentials: SELECTEL      # BOOTSTRAP_SELECTEL_ACCESS_KEY_ID, BOOTSTRAP_SELECTEL_SECRET_ACCESS_KEY
  - name: github
    type: git
    push_url: git@github.com:konsierge/bootstrap.git
    branch: master
    public_url: https://raw.githubusercontent.com/konsierge/bootstrap/master/dist/
    credentials: GITHUB        # BOOTSTRAP_GITHUB_DEPLOY_KEY — путь к приватному SSH deploy-ключу с правом записи
```

Endpoint и регион Selectel сверить с документацией через Context7 при реализации; публичный URL файла = `public_url + <env>.jws`.
Для `git` push идёт с `GIT_SSH_COMMAND="ssh -i $BOOTSTRAP_GITHUB_DEPLOY_KEY -o IdentitiesOnly=yes"`.

Готово, когда: тесты с поддельным S3 (`botocore.stub.Stubber` или `moto`, выбрать одно) и поддельным HTTP (`respx`) покрывают
`unchanged`, `upload`, `rollback_refused`, `missing_credentials`, `unreachable` в dry-run, проверку после записи и `stale_cache`
для `git` (паузы подменяются), отсутствие любых записей в `--dry-run`, `--secrets` через подменённый адаптер `age`;
`bin/publish --dry-run` в репозитории без секретов завершается с выходом `0` при недоступных зеркалах.

### 4. `bin/check-mirrors`, CI, README

Файлы: `bootstrap/check_mirrors.py`, `bin/check-mirrors`, `.gitlab-ci.yml`, `README.md`, `tests/test_check_mirrors.py`.

`.gitlab-ci.yml`: образ `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` (с `git`), кеш `uv`; stages `check`, `publish`;
jobs `test` (`uv run pytest`, `uv run ruff check`, `uv run ruff format --check`), `check` (`bin/check --base <sha>`;
на MR — `CI_MERGE_REQUEST_DIFF_BASE_SHA`, на `master` — `CI_COMMIT_BEFORE_SHA`, нулевой SHA первого push → без `--base`;
`GIT_DEPTH: 0`), `publish:dry-run` (MR), `publish` (`master`, `resource_group: publish`, секреты — protected masked variables,
deploy-ключ — переменная типа File).

README: назначение, установка (`uv`, `age`), структура, поток изменения (`design.md` §5), команды с примерами, генерация ключей
и хранение копий, аварийная публикация `--secrets`, **секреты**: таблица переменных (имя, цель, вид, где хранится: GitLab CI
protected/masked, File для deploy-ключа; локально — `age`-зашифрованный dotenv), права сервисных аккаунтов (запись в один бакет),
что создать до первой публикации (бакеты с публичным чтением `v1/`, сервисные аккаунты, GitHub-репозиторий `konsierge/bootstrap`
с deploy-ключом на запись, CNAME `bootstrap.ai-konsierge.ru` с сертификатом провайдера бакета), cron для `check-mirrors`
с push в Uptime Kuma.

Готово, когда: тесты `check-mirrors` с поддельным HTTP покрывают все коды таблицы спеки и формат `json`; CI проходит GitLab CI
Lint (`mcp__gitlab__validate_ci_lint`, без доступа к проекту — только содержимое); README покрывает пункты критерия 7.

### 5. Шаг владельца (стоп deploy)

После ревью и исправлений, перед гейтом. Владелец в своём терминале в корне репозитория:

```
bin/keygen prod-2026a
bin/keygen prod-2026b
bin/keygen stage-2026a
bin/sign staging --kid stage-2026a
bin/sign production --kid prod-2026a
```

Затем оркестратор проверяет `bin/check`, коммитит `keys/*.pub.json`, `manifests/*.json`, `dist/*.jws`
(`feat: add signing keys and signed manifests v1`) и продолжает с гейта. Приватные файлы и парольные фразы агент не читает и не запрашивает.

## Проверка

Базовая линия до среза 1: проверок нет (пустой репозиторий).

После каждого среза и в гейте:

```
uv run pytest
uv run ruff check
uv run ruff format --check
```

Дополнительно в `verification.md` (не гейт, ожидаемые результаты):

- `bin/verify testdata/valid.jws --env production --keys testdata/keys --now <now из vectors.json>` → выход `0`;
- `bin/publish --dry-run` → выход `0`, план `upload`, предупреждения `unreachable`;
- `bin/check-mirrors` → выход `1`, `unreachable`/`http_error` по каждому URL, без трассировки исключения;
- GitLab CI Lint по `.gitlab-ci.yml` → valid.

После шага владельца — `bin/check` выход `0` (входит в гейт).

## Полномочия и передача

- **Реализация разрешена:** да — постановка задачи пользователем 2026-11-02 («через deploy реализовать этапы 0 и 1»).
- **Task branch:** `KON-11445`
- **Delivery target:** локальный merge `KON-11445` → `master` (ответ пользователя 2026-11-02). Ветки `dev` нет и она не создаётся.
- **Review tier:** `fable` (контракт подписи и публичный контракт для клиентов).
- **Gate:** `uv run pytest`; `uv run ruff check`; `uv run ruff format --check`; `bin/check` (после шага 5).
- **Excluded actions:** добавление remote (GitLab `digitalbox/backend/instruments/bootstrap`, GitHub `konsierge/bootstrap`), любой push,
  реальная публикация на зеркала, создание облачных ресурсов, генерация настоящих ключей и подпись агентом, правки в репозитории `traefik`.
- **Плановый стоп:** срез 5 — шаг владельца.
- **Next session:** `/deploy docs/requests/client-bootstrap-config` (на ветке `KON-11445`).
