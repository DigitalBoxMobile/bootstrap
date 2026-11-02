# Backlog

- **Health-роутер в Traefik для проб клиента.** Сейчас клиент считает домен живым по любому HTTP-ответу Traefik
  (`docs/requests/client-bootstrap-config/spec.md`, «Контракт клиента»). Добавить в репозитории `traefik` роутер
  `Path(/healthz)` → `ping@internal` (`--ping=true`) для всех хостов базовых доменов, после чего клиент сможет требовать 200.
- **Создать зеркала и секреты публикации** (этап 1, ручная часть): бакеты Yandex и Selectel, сервисные аккаунты,
  GitHub `konsierge/bootstrap` с deploy-ключом, CNAME `bootstrap.ai-konsierge.ru`, переменные GitLab CI — по README.
- **Развернуть `bin/check-mirrors` на `proxy`** по cron с push в Uptime Kuma.
