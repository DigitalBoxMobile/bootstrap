# Bootstrap-манифесты: этапы 0 и 1 — спецификация

Статус: `ready for implementation`

Архитектура и обоснования — `design.md` рядом (решения §9). Эта спека фиксирует, что строится в этапах 0 и 1 из §8,
и контракт для клиентских реализаций этапа 2. Контекст инцидента — `../traefik/docs/requests/dns-provider-resilience/`.

## Что и зачем

Репозиторий получает всё, чтобы владелец мог подписать манифест офлайн-ключом, а CI или ноутбук — проверить его
и разложить байт в байт по независимым зеркалам, а внешний монитор — сверить зеркала с репозиторием:

- **Этап 0:** JSON Schema манифеста v1, генерация ключей, `bin/sign`, `bin/verify`, манифесты `production` и `staging`,
  подписанные `dist/*.jws`, тест-векторы и документ контракта для клиентов.
- **Этап 1:** `mirrors.yml`, `bin/publish` (Yandex Object Storage, Selectel Object Storage, GitHub raw) с `--dry-run`,
  `bin/check-mirrors` для внешнего монитора, `.gitlab-ci.yml`, README с описанием секретов.

## Инструменты (решение сессии проектирования)

- Python ≥ 3.13, проект под `uv` (`pyproject.toml` + `uv.lock`), пакет `bootstrap/` в корне репозитория.
  `bin/*` — тонкие обёртки `exec uv run --frozen --project <repo> python -m bootstrap.<cmd> "$@"`.
- JOSE — `jwcrypto` (Ed25519 через `cryptography`); схема — `jsonschema` (Draft 2020-12); YAML — `PyYAML`; S3 — `boto3`;
  HTTP — `httpx`. Своей криптографии нет. Тесты — `pytest`, линтер — `ruff`.
- Шифрование приватных ключей и файла секретов публикации — CLI `age` (`brew install age`), вызывается подпроцессом;
  парольную фразу `age` читает с TTY сам. CI `age` не нужен.
- Никакой шаг (`sign`, `verify`, `check`, `publish`, `check-mirrors`) не обращается к GitLab или `konsierge.com`.
  Сеть нужна только `publish` (зеркала) и `check-mirrors`; `uv` при первом запуске ставит зависимости с PyPI.

## Манифест v1

Файл `manifests/<env>.json`, `env ∈ {production, staging}`. Поля и смысл — `design.md` §1. Схема `schema/manifest.v1.json`:

| Поле | Обязательно | Правило |
| --- | --- | --- |
| `schema` | да | `const 1` |
| `env` | да | `production` \| `staging` |
| `version` | да | целое ≥ 1 |
| `issued_at`, `expires_at` | да | RFC 3339 UTC `YYYY-MM-DDTHH:MM:SSZ`; `expires_at > issued_at` проверяет `bin/check` |
| `base_domains` | да | ≥ 1, уникальные, hostname в нижнем регистре без схемы, порта и точки в конце |
| `health_path` | да | начинается с `/` |
| `mirrors` | да | ≥ 1, уникальные, `^https://` URL |
| `policy` | да | `connect_timeout_ms`, `probe_interval_s`, `refresh_interval_s` — целые > 0, все обязательны |
| `min_app_version` | нет | `{ios, android}` строки |
| `notice` | нет | `{ru, en}` строки |

На верхнем уровне и в `policy` разрешены дополнительные поля (`additionalProperties: true`): расширения из §1 «Отложено»
совместимы с v1, клиент незнакомые поля игнорирует.

Начальное содержимое (`version: 1` в обоих):

- `base_domains: ["konsierge.com", "ai-konsierge.ru"]` для обоих окружений (staging-хосты имеют вид `<host>-staging.<base>`,
  базовые домены общие).
- `health_path: "/healthz"`. Вход клиентов — через Traefik (уточнение пользователя 2026-11-02); собственного health-роутера
  у Traefik сейчас нет, поэтому контракт считает домен живым при **любом HTTP-ответе** (любой статус) после успешного TLS
  на `https://<api-host>.<domain><health_path>` — это и проверяет путь DNS → TLS → Traefik. Роутер `/healthz` → `ping@internal`
  на всех базовых доменах — пункт `BACKLOG.md` (репозиторий `traefik`), после него статус можно будет учитывать без смены схемы.
- `policy`: `connect_timeout_ms 3000`, `probe_interval_s 600`, `refresh_interval_s 21600` (§9, открытый пункт принят как стартовый).
- `min_app_version: {"ios": "0", "android": "0"}`, `notice: {"ru": "", "en": ""}`.
- `mirrors` (`<env>` = имя окружения):
  `https://storage.yandexcloud.net/konsierge-bootstrap/v1/<env>.jws`,
  `https://konsierge-bootstrap.selstorage.ru/v1/<env>.jws`,
  `https://raw.githubusercontent.com/konsierge/bootstrap/master/dist/<env>.jws`,
  `https://bootstrap.ai-konsierge.ru/v1/<env>.jws`.
  Бакеты и CNAME пока не созданы; README перечисляет, что должно существовать до первой публикации.

## Подпись: формат файла `dist/<env>.jws`

- JWS **General JSON Serialization** всегда, даже с одной подписью (клиент разбирает одну форму):
  `{"payload":"<b64url>","signatures":[{"protected":"<b64url>","signature":"<b64url>"}, …]}`.
  Без `header` (незащищённого заголовка). Файл — компактный JSON (без пробелов) и завершающий `\n`.
- `payload` — base64url без паддинга от байтов компактной UTF-8 сериализации манифеста
  (`json.dumps(m, ensure_ascii=False, separators=(",", ":"))`, порядок ключей как в исходном файле).
  Эти байты однозначно выводятся из `manifests/<env>.json`; `bin/check` требует точного совпадения.
- Protected header — ровно `{"alg":"EdDSA","kid":"<kid>"}`.
- Подписываемые байты — `ASCII(protected + "." + payload)`, Ed25519 (RFC 8037). Подпись детерминирована: повторная подпись
  того же манифеста тем же ключом даёт те же байты.
- Размер файла ≤ 3584 байт (`bin/sign` и `bin/check` отказывают при превышении).

## Ключи

- `kid` — `^(prod|stage)-[0-9]{4}[a-z]$`; префикс задаёт окружение: `prod` → `production`, `stage` → `staging`.
  Начальный набор: `prod-2026a` (активный), `prod-2026b` (запасной), `stage-2026a`.
- Публичный ключ — `keys/<kid>.pub.json`: JWK `{"kty":"OKP","crv":"Ed25519","x":…,"kid":…,"use":"sig","alg":"EdDSA"}`,
  без `d`. Набор доверенных ключей окружения = все файлы `keys/` с его префиксом.
- Приватный ключ — JWK, зашифрованный `age -p` (парольная фраза), по умолчанию `~/.config/konsierge/bootstrap/<kid>.jwk.age`
  (каталог переопределяется `BOOTSTRAP_KEY_DIR`). Внутри рабочего дерева репозитория `bin/keygen` писать отказывается.
  `.gitignore` исключает `*.age`, `*.jwk` и `*.key`.
- **Настоящие ключи генерирует и подписывает только владелец** в своём терминале (подтверждено пользователем при проектировании):
  приватный материал и парольные фразы не проходят через сессию агента. Тест-векторы используют эфемерные ключи,
  приватные половины которых не сохраняются.

## Команды

Общие правила: ошибки — в stderr одной строкой `error: <reason_code>: <детали>`, выход `1`; неверные аргументы — выход `2`.

### `bin/keygen <kid>`

Проверяет формат `kid` и отсутствие `keys/<kid>.pub.json` и файла приватного ключа. Генерирует Ed25519 JWK, передаёт приватный
JWK в stdin `age -p -o <путь>`, после успеха `age` пишет `keys/<kid>.pub.json`. Приватный ключ не попадает на диск в открытом виде.
Печатает пути и напоминание о копиях (Passbolt, две офлайн-копии; запасной — отдельно).

### `bin/sign <env> --kid <kid> [--kid <kid>…]`

1. Читает `manifests/<env>.json`, проставляет `issued_at = now` и `expires_at = now + 180 дней` (UTC, секунды) и перезаписывает файл
   (JSON с отступом 2, завершающий `\n`, порядок ключей сохраняется).
2. Валидирует схему; `env` в файле равен аргументу; каждый `kid` принадлежит окружению и имеет файл в `keys/`.
3. Монотонность: если `dist/<env>.jws` существует, его `version` строго меньше новой; иначе `version ≥ 1`.
4. Для каждого `kid` расшифровывает приватный ключ `age -d` (stdout в память), проверяет, что его публичная часть совпадает
   с `keys/<kid>.pub.json`, подписывает. Порядок подписей — порядок `--kid`.
5. Пишет `dist/<env>.jws`, затем прогоняет на нём ту же проверку, что `bin/verify`, и печатает итог.

При ошибке на любом шаге `dist/` не меняется (манифест с обновлёнными датами может остаться — это ожидаемо).

### `bin/verify <file.jws> --env <env> [--keys <dir>] [--min-version N] [--now <rfc3339>]`

Референсная проверка контракта (ниже). `--keys` по умолчанию `keys/`; `--min-version` — сохранённая версия клиента (по умолчанию 0);
`--now` — для детерминированной оценки срока. Успех: выход `0`, в stdout JSON
`{"accepted":true,"env":…,"version":…,"valid_kids":[…],"expires_at":…,"expired":bool}`.
Отказ: выход `1`, stdout `{"accepted":false,"reason":"<code>"}`.

### `bin/check [--base <git-ref>]`

Проверка репозитория для CI и локально (сеть не нужна):

- каждый `keys/*.pub.json` — валидный публичный Ed25519 JWK, `kid` совпадает с именем файла и форматом, нет `d`;
- в `keys/` нет других файлов, кроме `*.pub.json`;
- для каждого `manifests/<env>.json`: схема, `env` = имя файла, `expires_at > issued_at`; `dist/<env>.jws` существует,
  проходит `bin/verify` ключами окружения, payload побайтно равен компактной сериализации манифеста, размер ≤ 3584;
- с `--base`: для каждого `env`, у которого `dist/<env>.jws` отличается от версии в `<base>` (через `git show`),
  новая `version` строго больше старой; файл, отсутствующий в `<base>`, проверяется только на `version ≥ 1`.
  Удаление `dist/<env>.jws` — ошибка.

Печатает по строке на проверку; выход `1` при любой ошибке.

### `bin/gen-testdata`

Создаёт эфемерные ключи и перезаписывает `testdata/` целиком (векторы ниже). Приватные половины живут только в памяти процесса.
Перегенерация меняет байты векторов — это допустимо, векторы и ожидания пишутся вместе.

### `bin/publish [--dry-run] [--env <env>…] [--target <name>…] [--secrets <file.age>]`

1. Прогоняет проверки `bin/check` (без `--base`) и останавливается при ошибке.
2. Для каждой пары (цель из `mirrors.yml`, `env`):
   - читает текущий объект: `s3` — `GetObject` по ключу `<prefix><env>.jws` (с учётными данными; в `--dry-run` без них —
     HTTP GET по `public_url`); `git` — HTTP GET по `public_url`;
   - байты равны локальным → `unchanged`;
   - объект есть и проходит проверку контракта с `version ≥` локальной, байты отличаются → `rollback_refused`, цель пропускается,
     итоговый выход `1`;
   - объекта нет или он не проходит проверку, или версия меньше → `upload` (в `--dry-run` только печать плана).
3. Запись: `s3` — `PutObject` с `Content-Type: application/jose+json`, `Cache-Control: public, max-age=300`;
   `git` — `git push <push_url> HEAD:refs/heads/<branch>` без force (неперематываемый push — ошибка цели). Для `git`
   публикуются сразу все `env`, перед push проверяется, что `HEAD` содержит именно эти `dist/` (рабочее дерево чистое по `dist/`).
4. После записи — HTTP GET по `public_url` и сравнение байтов; для `git` до 10 попыток с паузой 30 с (кеш raw), после них —
   предупреждение `stale_cache`, не ошибка.
5. Итог — таблица `target env action result`; выход `1`, если хоть одна цель завершилась ошибкой (недоступность зеркала при чтении
   в `--dry-run` — предупреждение `unreachable`, не ошибка).

`--secrets <file.age>`: расшифровывает `age -d` dotenv-файл с переменными из README и подмешивает их в окружение процесса
(аварийный путь с ноутбука). Без `--secrets` переменные берутся из окружения (CI). Отсутствующие учётные данные цели вне
`--dry-run` — ошибка `missing_credentials` этой цели.

### `bin/check-mirrors [--env <env>…] [--format text|json] [--expire-warn-days 60] [--now <rfc3339>]`

Для внешнего монитора (cron на `proxy` рядом с Uptime Kuma, клон репозитория, обновляемый из GitHub-зеркала). Для каждого `env`
берёт эталон `dist/<env>.jws` из рабочего дерева и проверяет **каждый URL из `mirrors` эталонного манифеста** (это ровно то,
что опрашивают клиенты, включая `bootstrap.ai-konsierge.ru`):

| Проверка | Код при нарушении |
| --- | --- |
| HTTP 200 за 10 с | `http_error` / `unreachable` |
| проходит контракт | `<reason_code>` контракта |
| байты равны эталону | `version_behind` (версия меньше), `version_ahead` (больше), `content_mismatch` (та же версия, другие байты) |
| `expires_at − now > --expire-warn-days` | `expiring` |

Выход `0` — всё в порядке, `1` — хоть одна проблема. `json` — массив `{env, url, ok, problems[]}`. Отправка в Uptime Kuma —
обёртка cron из README (`bin/check-mirrors && curl -fsS <push-url>`), развёртывание на `proxy` — вне задачи.

## Контракт клиента (этап 2)

Переносится в `docs/client-contract.md` (поддерживаемый документ; `design.md` §3 остаётся обоснованием).
Вход: байты файла, `env` сборки, доверенные ключи этого `env` (встроенные JWK), сохранённая версия `stored_version` (0, если нет),
текущее время только для флага срока. Проверки по порядку, первый провал даёт код отказа:

| # | Проверка | Код |
| --- | --- | --- |
| 1 | Байты — JSON-объект с `payload` (строка) и `signatures` (непустой массив объектов с `protected` и `signature`); base64url декодируется | `malformed` |
| 2 | Для каждой подписи: protected header — объект с `alg == "EdDSA"`, строковым `kid`, без `crit`; иначе подпись пропускается | — |
| 3 | Ни у одной подписи `kid` не входит в доверенный набор | `unknown_key` |
| 4 | Ни одна подпись с доверенным `kid` не проходит Ed25519-verify над `ASCII(protected + "." + payload)` | `bad_signature` |
| 5 | Payload — UTF-8 JSON-объект; `schema` — целое | `malformed` |
| 6 | `schema != 1` | `unknown_schema` |
| 7 | Структура не соответствует таблице полей v1 (обязательные поля, типы, `https://`, непустые списки) | `invalid_manifest` |
| 8 | `env != env сборки` | `env_mismatch` |
| 9 | `version < stored_version` | `rollback` |

Выбор домена (`design.md` §3): домен живой, если на `https://<api-host>.<domain><health_path>` за `connect_timeout_ms`
установлено TLS-соединение и получен любой HTTP-ответ; статус не учитывается.

Принятый манифест возвращается с флагом `expired = now ≥ expires_at`: срок мягкий, просроченный манифест **принимается**.
Достаточно одной верной подписи доверенного ключа (порог 1); неверные или чужие подписи рядом с верной не мешают.
Размер файла клиент не проверяет (лимит — свойство публикации).

### Тест-векторы `testdata/`

- `testdata/keys/` — публичные JWK тестового набора: `prod-9999a` (доверенный production), `stage-9999a` (доверенный staging).
  Ключ «чужого» подписанта в `keys/` не попадает.
- `testdata/vectors.json` — массив `{name, file, env, trusted_kids, stored_version, now, expect}`, где
  `expect = {accepted: true, version, expired}` или `{accepted: false, reason}`.
- Файлы `testdata/<name>.jws`, по одному на вектор:

| `name` | Что внутри | Ожидание |
| --- | --- | --- |
| `valid` | production, подписан `prod-9999a` | accepted, `expired: false` |
| `tampered_payload` | `valid` с изменённым байтом payload (base64url остаётся валидным) | `bad_signature` |
| `foreign_key` | подписан чужим ключом, `kid` = `prod-9999a` | `bad_signature` |
| `unknown_kid` | подписан чужим ключом с `kid` = `prod-9999z` | `unknown_key` |
| `rollback` | валидный `version: 5`, `stored_version: 6` | `rollback` |
| `wrong_env` | payload `env: staging`, подписан `prod-9999a`, проверка как production | `env_mismatch` |
| `staging_key_in_prod` | staging-манифест, подписан `stage-9999a`, проверка как production | `unknown_key` |
| `expired` | `expires_at` раньше `now` вектора | accepted, `expired: true` |
| `rotation` | две подписи: `prod-9999z` (чужой) и `prod-9999a` | accepted |
| `bad_mirror_scheme` | подписан `prod-9999a`, зеркало `http://…` | `invalid_manifest` |
| `unknown_schema` | подписан `prod-9999a`, `schema: 2` | `unknown_schema` |
| `malformed` | не JSON | `malformed` |

## Сценарии и ошибки

- Правка домена: человек меняет `manifests/production.json`, `version += 1` → владелец `bin/sign production --kid prod-2026a` →
  коммит → CI на MR зелёный → merge → CI публикует. Без GitLab: `bin/publish --secrets ~/…/publish.env.age` с ноутбука.
- `bin/sign` с неизменённой `version` → `error: version_not_increased`, `dist/` не тронут.
- Ручная правка `dist/*.jws` или манифеста без переподписи → `bin/check` падает (`payload_mismatch` или `bad_signature`).
- Компрометация зеркала, на которое положили старую версию: `bin/publish` перезапишет её (`version` ниже); положили бо́льшую
  чужую версию — подписать её нельзя, она не проходит контракт и тоже перезаписывается; монитор сообщает о расхождении.
- Зеркало недоступно при публикации → ошибка этой цели, остальные публикуются, выход `1`.
- CI без секретов публикации (сейчас) → job `publish` падает с `missing_credentials`; это ожидаемо до появления бакетов.

## Критерии приёмки

1. `uv run pytest` зелёный: проверки контракта на всех векторах `testdata/vectors.json`; независимая проверка подписей векторов
   «сырым» Ed25519 через `cryptography` без `jwcrypto` (доказывает, что контракт реализуем без JOSE-библиотеки);
   юнит-тесты `sign` (эфемерный ключ, без `age`), `check` (включая `--base` на временном git-репозитории),
   `publish` (поддельный S3 и HTTP, все ветки: `unchanged`, `upload`, `rollback_refused`, `missing_credentials`, dry-run без записи),
   `check-mirrors` (поддельный HTTP, все коды).
2. `uv run ruff check` и `uv run ruff format --check` чистые.
3. После шага владельца: `keys/` содержит три публичных JWK, `dist/production.jws` и `dist/staging.jws` подписаны
   `prod-2026a` и `stage-2026a`, `bin/check` выход `0`, размер каждого файла ≤ 3584.
4. `bin/publish --dry-run` без секретов выходит `0` с планом `upload` для всех целей (зеркал нет — `unreachable` как предупреждение).
5. `bin/check-mirrors` выходит `1` с `unreachable`/`http_error` по каждому URL (зеркал ещё нет) и не падает с исключением.
6. `.gitlab-ci.yml` проходит GitLab CI Lint (`mcp__gitlab__validate_ci_lint`, только чтение); jobs: `test`, `check`
   (с `--base` от `CI_MERGE_REQUEST_DIFF_BASE_SHA` на MR и `CI_COMMIT_BEFORE_SHA` на master), `publish:dry-run` на MR,
   `publish` на `master` с `resource_group: publish`.
7. README описывает: установку (`uv`, `age`), поток изменения, команды, аварийную публикацию, точный список секретов и их вид,
   что создать у провайдеров перед первой публикацией, cron для `check-mirrors`.
8. `docs/client-contract.md` и `testdata/README.md` описывают контракт и формат векторов так, чтобы клиентская команда
   реализовала проверку без чтения Python-кода.

## Объём

Входит: всё выше, в этом репозитории. Не входит: клиентские библиотеки iOS/Android, создание бакетов, сервисных аккаунтов, CNAME,
GitHub-репозитория и deploy-ключа, развёртывание монитора на `proxy`, push-канал, VK Cloud, домен-маяк, генерация манифеста
из инвентаря.

## Подтверждённые решения

- Формат, алгоритм, ключевая модель, зеркала первой итерации, CI без ключей — `design.md` §9 и постановка задачи пользователем.
- Инструменты: `jwcrypto` (разрешён пользователем наравне со step CLI), `uv`, `age` CLI — выбор сессии проектирования.
- Ключи `prod-2026a`, `prod-2026b`, `stage-2026a` генерирует и dist подписывает владелец сам — ответ пользователя 2026-11-02.
- Ветка `KON-11445`, доставка — локальный merge в `master`; remote не добавлять, push нет — ответ пользователя 2026-11-02.
- Вход через Traefik, gateway/chat не используются — уточнение пользователя 2026-11-02; отсюда проба «любой HTTP-ответ».
- `health_path: "/healthz"`, общие `base_domains` для staging, имена бакетов и URL зеркал из `design.md` §1 — выбор сессии
  проектирования по данным репозиториев; меняются переподписью манифеста.

## Открытые вопросы

Нет. Точные endpoint-ы и публичные домены бакетов уточняются при их создании правкой `mirrors.yml` и манифестов.
