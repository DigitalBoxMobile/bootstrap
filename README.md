# bootstrap

Подписанные bootstrap-манифесты клиентов Konsierge: актуальные базовые домены и адреса сервисов,
которые приложения получают во время работы из нескольких независимых зеркал вместо констант сборки.
Манифест подписан Ed25519 (JWS), ключи проверки встроены в клиенты; зеркала подделать нельзя.

- Архитектура и решения: `docs/adr/0001-signed-bootstrap-manifests.md`.
- Контракт клиента (проверки, коды отказа): `docs/client-contract.md`.
- Тест-векторы для клиентских реализаций: `testdata/README.md`.

## Установка

```sh
mise install       # python 3.13, uv и age закреплены в mise.toml
mise run setup     # uv sync --frozen
```

Задачи `mise run <имя>`:

| Задача | Что делает |
| --- | --- |
| `setup` | установка Python-зависимостей |
| `test` | `pytest` |
| `lint` | `ruff check` и `ruff format --check` |
| `fmt` | `ruff format` |
| `check` | `bin/check` |
| `ci` | `test`, `lint`, `check` — всё, что CI запускает до публикации |
| `mirrors` | `bin/check-mirrors` |
| `publish:dry-run` | `bin/publish --dry-run` |
| `publish` | `bin/publish --secrets ~/.config/konsierge/bootstrap/publish.env.age` |

Команды `bin/*` лежат в `bin/` и запускаются из любого каталога. Для CI и проверок нужен только `uv`;
`age` нужен там, где подписывают (`keygen`, `sign`) или читают зашифрованные секреты (`publish --secrets`).

## Структура

```
manifests/   источник манифестов по окружениям (production, staging)
dist/        подписанные JWS — публикуемые байты, меняются только через bin/sign
keys/        публичные ключи (JWK); их же встраивают клиенты. Приватных ключей здесь нет
schema/      JSON Schema манифеста
mirrors.yml  цели публикации (S3 Yandex, S3 Selectel, git-зеркало GitHub)
bin/         keygen, sign, verify, check, gen-testdata, publish, check-mirrors
testdata/    тест-векторы для iOS/Android
docs/        контракт клиента, ADR
```

## Поток изменения

Источник правды — GitHub `DigitalBoxMobile/bootstrap`, ветка `main`; CI — GitHub Actions (`.github/workflows/`).

1. Правка `manifests/<env>.json`, `version += 1` — pull request.
2. Владелец ключа: `bin/sign <env> --kid <kid>` (парольная фраза `age`), новый JWS в `dist/`; коммит в тот же PR.
3. Workflow `ci` на PR: `mise run ci` (тесты, линтер, `bin/check`), `bin/check --base <база PR>` (`version` строго растёт),
   `bin/publish --dry-run`.
4. Merge в `main` — это и есть публикация на GitHub-зеркало (raw). После зелёного `ci` на `main` workflow `publish`
   (environment `production`, без параллельных запусков) раскладывает `dist/*.jws` байт в байт в Yandex и Selectel
   условной записью и сравнивает зеркала с `dist/`.
5. Без GitHub Actions и `konsierge.com`: тот же `bin/publish` с ноутбука (см. «Аварийная публикация»).

CI не подписывает, а только проверяет и публикует. Ручная правка `dist/*.jws` запрещена.

## Команды

```sh
bin/keygen prod-2026a                      # новый ключ: приватная половина age-зашифрована вне репозитория, keys/prod-2026a.pub.json в репозитории
bin/sign production --kid prod-2026a       # подписать manifests/production.json -> dist/production.jws
bin/sign production --kid prod-2026a --kid prod-2026b   # две подписи (ротация)
bin/verify dist/production.jws --env production         # проверка по контракту клиента, JSON-результат
bin/check                                  # согласованность репозитория без сети
bin/check --base origin/master             # плюс: version не ниже и не равна версии в базе
bin/gen-testdata                           # пересобрать testdata/ (тестовые ключи, не боевые)
bin/publish --dry-run                      # план публикации без записи
bin/publish --env staging --target yandex  # публикация части целей
bin/check-mirrors                          # монитор зеркал
bin/check-mirrors --env production --format json --expire-warn-days 30
```

`bin/check-mirrors` берёт эталон `dist/<env>.jws` из рабочего дерева и проверяет каждый URL из `mirrors` манифеста:
HTTP 200 за 10 с (`http_error`, `unreachable`), прохождение контракта (код отказа контракта), байты равны эталону
(`version_behind`, `version_ahead`, `content_mismatch`), срок `expires_at` дальше порога (`expiring`, по умолчанию 60 дней).
Ответ зеркала длиннее 65536 байт — `oversized`; неожиданное исключение при проверке одного URL — `internal_error`
(с типом исключения), остальные URL проверяются дальше.
Выход `0` — всё в порядке, `1` — есть проблема; `--format json` печатает массив `{env, url, ok, problems[]}`.

`bin/publish` перед записью читает зеркало (preflight) и сообщает по каждой паре env/цель результат: `unchanged`,
`rollback_refused`, `upload`, либо ошибку:

- `rollback_refused` — на зеркале манифест с `version` не ниже локальной: валидный или подписанный только ключами,
  которых нет в локальном `keys/` ни для одного env (подпись такого объекта не проверить, сравнивается только
  `version`). Объект с известным `kid`, отклонённый контрактом (ключ другого env, `alg: none`), перезаписывается.
  Цель не пишется, выход `1`. Обычно это устаревший клон после ротации ключей: обновить его (`git pull`) и повторить.
  Если после обновления отказ остаётся, на зеркале посторонний объект: любой, у кого есть запись в бакет, может так
  заблокировать публикацию, и флага принудительной записи нет — это осознанная цена защиты от отката. Проверить объект
  (`curl -s <public_url><env>.jws > /tmp/remote.jws && bin/verify /tmp/remote.jws --env <env>`), отозвать
  утёкшие ключи бакета, удалить объект вручную и повторить публикацию.
- `conflict` — условная запись в S3 отклонена (`412`): объект изменился после preflight; повторить публикацию.
- `env_filter_conflict` — git-зеркало публикует весь `HEAD`, поэтому `--env` не может исключить env, чей `dist/`
  на git-зеркале отличается; цель не пушится, выход `1`.
- `oversized` — ответ зеркала при preflight длиннее 65536 байт.
- `internal_error` — неожиданное исключение при проверке URL; тип исключения в деталях.

Запись в S3 условная (`If-Match` с ETag из preflight, `If-None-Match: *` для нового объекта), поэтому
конкурентные издатели не могут откатить зеркало. Git-цель push-ит без force.

## Ключи

Приватные ключи в репозиторий не попадают ни в каком виде, включая зашифрованный.
`bin/keygen` пишет их в `~/.config/konsierge/bootstrap/<kid>.jwk.age` (или `$BOOTSTRAP_KEY_DIR`).

- Production: рабочий `prod-2026a` и запасной `prod-2026b`; staging: `stage-2026a`.
- Копии каждого приватного ключа: Passbolt и две офлайн-копии (разные носители, разные места).
- Запасной ключ хранится отдельно от рабочего. Оба публичных ключа встроены в сборки заранее.
- Парольная фраза `age` — в Passbolt, отдельно от файла ключа.

Плановая ротация: новый ключ добавляется в сборки → после достаточного охвата манифест подписывается старым и новым
(`--kid` дважды) → старый убирается из подписи → из сборок. Компрометация активного prod-ключа: сразу подписывать запасным
и выпустить сборку с новым запасным; сборки со скомпрометированным ключом уязвимы до обновления (`min_app_version`).

## Правила эксплуатации

- Домены из любого манифеста за последние 3 года продлеваются; исключение домена из `base_domains` — отдельный шаг с бампом `version`.
- Переподписать манифест до истечения `expires_at` (`check-mirrors` предупреждает за 60 дней); `bin/sign` ставит срок 180 дней.

## Аварийная публикация с ноутбука

Без GitHub Actions и `konsierge.com`: секреты читаются из `age`-зашифрованного dotenv.

```sh
# один раз: файл с переменными из таблицы «Секреты», по строке NAME=value
# (BOOTSTRAP_GITHUB_DEPLOY_KEY — путь к приватному SSH-ключу)
mkdir -p ~/.config/konsierge/bootstrap
age -p -o ~/.config/konsierge/bootstrap/publish.env.age publish.env
rm -P publish.env

bin/publish --secrets ~/.config/konsierge/bootstrap/publish.env.age
```

## Секреты

Публикация читает пять переменных. Сервисные аккаунты имеют право записи только в один бакет своего провайдера.

| Имя | Цель | Вид | Где хранится |
| --- | --- | --- | --- |
| `BOOTSTRAP_YANDEX_ACCESS_KEY_ID` | Yandex Object Storage | статический ключ доступа S3 сервисного аккаунта с записью в один бакет | CI: секрет environment `production` в GitHub; локально: в `age`-dotenv; копия в Passbolt |
| `BOOTSTRAP_YANDEX_SECRET_ACCESS_KEY` | Yandex Object Storage | секрет того же ключа | то же |
| `BOOTSTRAP_SELECTEL_ACCESS_KEY_ID` | Selectel Object Storage | статический ключ доступа S3 сервисного аккаунта с записью в один бакет | то же |
| `BOOTSTRAP_SELECTEL_SECRET_ACCESS_KEY` | Selectel Object Storage | секрет того же ключа | то же |
| `BOOTSTRAP_GITHUB_DEPLOY_KEY` | git-зеркало `DigitalBoxMobile/bootstrap` | приватный SSH deploy-ключ с правом записи | только для аварийной публикации с ноутбука (CI не пушит: merge в `main` уже публикация); путь к файлу в `age`-dotenv; копия в Passbolt |

Push в git-зеркало идёт по SSH с проверкой ключа хоста: `known_hosts` в корне репозитория (поле `known_hosts` цели в `mirrors.yml`)
содержит опубликованные ключи `github.com` (ed25519, ecdsa, rsa). Перед push `publish` копирует deploy-ключ во временный файл с правами
`0600` (исходный файл может иметь другие права) и удаляет его после; `StrictHostKeyChecking=yes`, `BatchMode=yes`.
Обновить `known_hosts` при ротации ключей GitHub:

```sh
curl -fsS https://api.github.com/meta | python3 -c 'import json,sys; [print("github.com", k) for k in json.load(sys.stdin)["ssh_keys"]]' > known_hosts
```

Сверить с https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints и закоммитить.

Секреты environment `production` доступны только workflow `publish` на `main` (правило deployment branches environment);
pull request, в том числе из форков, их не получают — `publish --dry-run` работает без секретов.

## Что создать до первой публикации

Не входит в этот репозиторий:

1. Бакеты `konsierge-bootstrap` в Yandex и Selectel с публичным чтением только префикса `v1/`.
   Yandex (сделано): облако `konsierge`, папка `bootstrap`, сервисный аккаунт `bootstrap-publisher` с ролью `storage.uploader`
   на папку; у бакета флаг публичного чтения объектов (`--public-read`, листинг выключен) и политика: анонимный `s3:GetObject`
   на `v1/*`, сервисному аккаунту `s3:GetObject`/`s3:PutObject` на `v1/*` и `s3:ListBucket` (без него отсутствующий объект
   отдаёт 403 вместо 404). Запрос, не попавший ни под одно правило политики, запрещён.
   Selectel (сделано): проект `bootstrap`, пул `ru-7` (endpoint `s3.ru-7.storage.selcloud.ru`), публичный бакет; анонимное чтение
   идёт только через публичный домен `83ae1846-0a7a-4209-8c6e-84ef458fa6ce.selstorage.ru` (листинг закрыт). Сервисный пользователь
   `bootstrap-publisher` с ролью `s3.user` на проект: сама роль доступа к объектам не даёт, его выдаёт политика бакета —
   `s3:GetObject`/`s3:PutObject` на `v1/*` и `s3:ListBucket`, плюс правило для администратора (политика действует и на панель).
2. GitHub-репозиторий `DigitalBoxMobile/bootstrap` (публичный, ветка `main`, ruleset запрещает удаление и force-push)
   и deploy-ключ с правом записи; приватная часть — в секреты. Актуальный `known_hosts` с ключами `github.com` лежит
   в репозитории (как обновить — в разделе «Секреты»).
3. Environment `production` в настройках GitHub-репозитория: deployment branches — только `main`; секреты
   `BOOTSTRAP_YANDEX_*` и `BOOTSTRAP_SELECTEL_*`. Ruleset `main`: обязательная проверка `ci`, без прямого push
   (исключение — deploy-ключ для аварийного пути). Копии секретов — в Passbolt.

Если меняется публичный URL зеркала, поправить `mirrors.yml` и `mirrors` в манифестах, поднять `version` и переподписать `dist/`.

## Мониторинг зеркал

На `proxy` рядом с Uptime Kuma: клон репозитория, обновляемый из GitHub-зеркала, и cron каждые 5 минут:

```cron
*/5 * * * * cd /opt/bootstrap && git pull -q --ff-only && bin/check-mirrors && curl -fsS https://<kuma-host>/api/push/<token>
```

Push в Uptime Kuma уходит только при выходе `0`; иначе Kuma поднимает алерт по отсутствию heartbeat.
Развёртывание монитора вне этого репозитория.
