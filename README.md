# bootstrap

Подписанные bootstrap-манифесты клиентов Konsierge: актуальные базовые домены и адреса сервисов,
которые приложения получают во время работы из нескольких независимых зеркал вместо констант сборки.
Манифест подписан Ed25519 (JWS), ключи проверки встроены в клиенты; зеркала подделать нельзя.

- Архитектура и решения: `docs/requests/client-bootstrap-config/design.md`.
- Контракт клиента (проверки, коды отказа): `docs/client-contract.md`.
- Тест-векторы для клиентских реализаций: `testdata/README.md`.

## Установка

```sh
brew install uv age
uv sync
```

Все команды лежат в `bin/` и запускаются из любого каталога. Для CI и проверок нужен только `uv`;
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
docs/        контракт клиента, дизайн, пакеты задач
```

## Поток изменения

1. Правка `manifests/<env>.json`, `version += 1` — MR.
2. Владелец ключа: `bin/sign <env> --kid <kid>` (парольная фраза `age`), новый JWS в `dist/`; коммит в тот же MR.
3. CI на MR: тесты, `bin/check --base <база MR>` (схема, подпись, `version` строго растёт), `bin/publish --dry-run`.
4. Merge в `master`: CI запускает `bin/publish` — `dist/*.jws` байт в байт во все цели из `mirrors.yml`,
   затем скачивает с каждого зеркала и сравнивает.
5. Без GitLab: тот же `bin/publish` с ноутбука (см. «Аварийная публикация»).

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
Выход `0` — всё в порядке, `1` — есть проблема; `--format json` печатает массив `{env, url, ok, problems[]}`.

## Ключи

Приватные ключи в репозиторий не попадают ни в каком виде, включая зашифрованный.
`bin/keygen` пишет их в `~/.config/konsierge/bootstrap/<kid>.jwk.age` (или `$BOOTSTRAP_KEY_DIR`).

- Production: рабочий `prod-2026a` и запасной `prod-2026b`; staging: `stage-2026a`.
- Копии каждого приватного ключа: Passbolt и две офлайн-копии (разные носители, разные места).
- Запасной ключ хранится отдельно от рабочего. Оба публичных ключа встроены в сборки заранее.
- Парольная фраза `age` — в Passbolt, отдельно от файла ключа.

## Аварийная публикация с ноутбука

Без GitLab и `konsierge.com`: секреты читаются из `age`-зашифрованного dotenv.

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
| `BOOTSTRAP_YANDEX_ACCESS_KEY_ID` | Yandex Object Storage | статический ключ доступа S3 сервисного аккаунта с записью в один бакет | CI: protected + masked; локально: в `age`-dotenv; копия в Passbolt |
| `BOOTSTRAP_YANDEX_SECRET_ACCESS_KEY` | Yandex Object Storage | секрет того же ключа | то же |
| `BOOTSTRAP_SELECTEL_ACCESS_KEY_ID` | Selectel Object Storage | статический ключ доступа S3 сервисного аккаунта с записью в один бакет | то же |
| `BOOTSTRAP_SELECTEL_SECRET_ACCESS_KEY` | Selectel Object Storage | секрет того же ключа | то же |
| `BOOTSTRAP_GITHUB_DEPLOY_KEY` | git-зеркало `konsierge/bootstrap` | приватный SSH deploy-ключ с правом записи | CI: переменная типа **File** (protected); локально: путь к файлу в `age`-dotenv; копия в Passbolt |

Переменные CI видны только защищённым веткам (`master`), поэтому MR их не получают: `publish:dry-run` работает без секретов.

## Что создать до первой публикации

Не входит в этот репозиторий:

1. Бакеты `konsierge-bootstrap` в Yandex и Selectel с публичным чтением только префикса `v1/`.
2. Сервисные аккаунты по одному на провайдера с записью в этот бакет; статические ключи — в секреты.
3. GitHub-репозиторий `konsierge/bootstrap` и deploy-ключ с правом записи; приватная часть — в секреты.
4. CNAME `bootstrap.ai-konsierge.ru` на один из бакетов, сертификат выпускает провайдер бакета.
5. Адреса Selectel в `mirrors.yml` (endpoint `s3.storage.selcloud.ru`, регион `ru-1`, публичный домен) подтвердить при создании
   бакета. Если URL изменились, поправить `mirrors.yml` и `mirrors` в манифестах и переподписать `dist/`.
6. Секреты в GitLab CI (protected + masked, deploy-ключ как File) и в Passbolt.

До появления бакетов `publish` в CI падает с `missing_credentials`, а `check-mirrors` сообщает `unreachable`/`http_error` — это ожидаемо.

## Мониторинг зеркал

На `proxy` рядом с Uptime Kuma: клон репозитория, обновляемый из GitHub-зеркала, и cron каждые 5 минут:

```cron
*/5 * * * * cd /opt/bootstrap && git pull -q --ff-only && bin/check-mirrors && curl -fsS https://<kuma-host>/api/push/<token>
```

Push в Uptime Kuma уходит только при выходе `0`; иначе Kuma поднимает алерт по отсутствию heartbeat.
Развёртывание монитора вне этого репозитория.
