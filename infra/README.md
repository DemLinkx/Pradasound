# Развёртывание Prada Sound

## Подготовка VPS

Нужен Linux VPS с Git, Docker Engine и плагином Compose v2 (команда
`docker compose`, с поддержкой `up --wait`). Установка Docker — по
[официальной инструкции для вашей ОС](https://docs.docker.com/engine/install/).
Откройте входящие TCP 80 и 443, настройте A/AAAA-записи домена. Порт 8000
открывать не нужно. Учётной записи запуска нужен доступ к Docker daemon.

В Compose два сервиса: `api` (один worker, лимит RAM 1 GiB) и `web` (Caddy,
256 MiB). Это начальные пределы, не результаты нагрузочного теста. Сборка
требует дополнительной RAM/диска; для маленького VPS используйте готовые образы.
Сертификаты Caddy хранятся в томе `caddy_data`, служебная конфигурация —
в `caddy_config`. API работает без постоянного тома, его состояние в памяти.

Если на VPS уже работают старые `prada-sound.service` и системный Caddy,
запланируйте миграцию: старые службы необходимо остановить перед Docker-запуском,
чтобы освободить порты 80/443. `infra/prada-sound.service` оставлен как файл
старой установки; для Docker он не используется. Старые сертификаты и `.env`
не удаляйте. Новый Caddy получит сертификат в собственном томе.

Пределы задаются через `API_MEMORY_LIMIT` и `WEB_MEMORY_LIMIT` в `.env`.
Для VPS с 1 GiB RAM используйте `512m` и `128m` соответственно, оставляя память
операционной системе и Docker. Это профиль небольшого MVP; его ёмкость под
нагрузкой отдельно не проверена.

## Первый запуск: готовые образы из GitHub

1. Загрузите проект в GitHub и дождитесь зелёного workflow на ветке `main`.
   Образы `…-api` и `…-web` появятся в GitHub Packages.
2. Для простого доступа можно сделать **оба** пакета публичными в их настройках.
   Если они приватные, на VPS выполните `docker login ghcr.io -u DemLinkx` и
   введите токен с `read:packages` в интерактивном запросе пароля. Для приватного
   Git-репозитория отдельно настройте read-only deploy key или GitHub-доступ.
3. Клонируйте проект на VPS и создайте конфигурацию:

```sh
git clone https://github.com/DemLinkx/Pradasound.git
cd Pradasound
cp .env.example .env
chmod 600 .env
```

Пример значений `.env` (пользователь и репозиторий в имени образа — строчными):

```dotenv
SITE_ADDRESS=music.example.com
CORS_ORIGINS=["https://music.example.com"]
HTTP_PORT=80
HTTPS_PORT=443
PRADA_IMAGE_PREFIX=ghcr.io/demlinkx/pradasound
PRADA_IMAGE_TAG=latest
```

Для воспроизводимого выпуска вместо `latest` укажите тег из отчёта workflow:
`sha-<полный SHA коммита>`. Оба образа должны иметь один и тот же тег.

```sh
./infra/deploy.sh pull
curl -fsS https://music.example.com/healthcheck
docker compose ps
```

`deploy.sh` сначала проверяет Compose, скачивает образы и только после успешного
скачивания пересоздаёт изменённые контейнеры. Он ждёт прохождения healthcheck.
Тест HTTP/WebSocket с компьютера разработчика:

```sh
uv run --project backend --locked python infra/smoke.py https://music.example.com
```

Скрипт создаёт временную комнату, проверяет общую очередь двух подключений,
skip, запрет чужого Origin и раздачу frontend. К провайдерам музыки не обращается.
После проверки протестируйте вручную вход по QR с телефона и воспроизведение
Яндекса/YouTube на устройстве хоста. Реальные аккаунты в CI не используются.

## Альтернатива: сборка на сервере

После клонирования и настройки домена оставьте `PRADA_IMAGE_PREFIX` пустым,
`PRADA_IMAGE_TAG=local`, затем:

```sh
./infra/deploy.sh build
```

Зависимости устанавливаются по `backend/uv.lock` и
`frontend/package-lock.json`; frontend собирается внутри Docker. Контейнер
`web` содержит готовую статику, Node.js в работающем контейнере отсутствует.
Dockerfile работает на amd64 и arm64; workflow GHCR собирает amd64 на GitHub runner.

## Обновления и откат

Порядок: изменения локально → тесты → commit/push → зелёный GitHub workflow →
обновление VPS. Автоматического SSH-деплоя нет.

```sh
git pull --ff-only
./infra/deploy.sh pull
curl -fsS https://music.example.com/healthcheck
```

Если используете фиксированные теги, перед `pull` замените `PRADA_IMAGE_TAG`
в `.env` на новый `sha-…`. При сборке на VPS используйте `build`.
Секретный `.env` не меняется от `git pull`.

Для отката готовых образов верните предыдущий `PRADA_IMAGE_TAG=sha-…` в `.env`
и выполните `./infra/deploy.sh pull`. Если выпуск также менял Compose/Caddy/
переменные, сначала переключите исходники на тот же предыдущий коммит.
При сборке из исходников переключите Git на предыдущий коммит и выполните `build`.
Не удаляйте предыдущие образы до проверки обновления.

Обновление backend закрывает WebSocket и очищает комнаты и авторизации Яндекса.
Планируйте его между вечеринками. Обновление только frontend не требует изменения
API; пересоздание `web` всё равно может переподключить WebSocket. Несколько
workers/реплик API использовать нельзя: у них будут разные очереди.

## Диагностика и обслуживание

```sh
docker compose ps
docker compose logs --tail=100 api web
docker compose logs -f --tail=50 api
docker compose restart api
docker compose stop
docker compose start
```

`restart api` очищает комнаты. Логи ограничены ротацией 3×10 MiB на сервис;
HTTP access log API выключен, чтобы не записывать одноразовые аудиоссылки.
`restart: unless-stopped` запускает контейнеры после перезагрузки Docker/VPS,
если они не были явно остановлены. Healthcheck обнаруживает неисправность,
но сам по себе не перезапускает зависший контейнер: проверьте логи и перезапустите
нужный сервис. Не используйте `docker compose down -v`: это удалит сертификаты.

Если HTTPS не запускается, проверьте DNS, входящие 80/443, отсутствие другого
сервиса на этих портах и `docker compose logs web`. При WebSocket 403 проверьте
точное совпадение схемы, домена и порта страницы с `CORS_ORIGINS` (без `/` в конце).
При изменении `.env` применяйте `docker compose up -d --wait`, а не `restart`:
старый контейнер не получает новые значения окружения от простого restart.

Резервируйте `.env` и тома Caddy отдельно от Git. Комнаты/сессии MVP не сохраняются
и резервной копии не имеют. Не публикуйте `.env`, cookies, токены и приватные ключи.

## Настройки

В `.env.example` перечислены используемые параметры. Необязательные
`YOUTUBE_API_KEY` и Telegram-настройки можно оставить пустыми. Яндекс авторизуется
на сайте по device code; общий пароль или токен Яндекса в `.env` не нужен.
По умолчанию Compose задаёт `YOUTUBE_AUDIO_ENABLED=false`: YouTube использует
iframe. Для экспериментального HTML5-аудио явно включите флаг.

### VPN для YouTube и Telegram

Профиль `vpn` запускает sing-box без TUN, прав администратора и открытых портов.
Лимит памяти — 96 MiB, целевой Go heap — 64 MiB. Яндекс остаётся на прямом
маршруте: глобальные HTTP_PROXY/HTTPS_PROXY задавать не нужно.

Сохраните полученную у провайдера VLESS-подписку (текст или base64) в приватный
файл `.local/blanc/subscription.txt`, затем создайте конфигурацию:

```sh
mkdir -p .local/blanc
chmod 700 .local/blanc
python3 infra/vpn_config.py .local/blanc/subscription.txt .local/blanc/config.json --nodes 14,10,0 --pinned-ports
```

Индексы узлов зависят от вашей подписки. Проверьте выбранные локации перед
использованием. В `.env` укажите:

```dotenv
COMPOSE_PROFILES=vpn
VPN_UID=1000
VPN_GID=1000
YOUTUBE_AUDIO_ENABLED=true
YOUTUBE_PROXY_URLS=["http://egress:1081","http://egress:1082","http://egress:1083"]
TELEGRAM_PROXY_URL=http://egress:1080
TELEGRAM_POLLING_ENABLED=true
```

VPN_UID/VPN_GID должны совпадать с владельцем приватного config.json (`id -u`,
`id -g` на сервере). Порты 1081–1083 закреплены за выбранными узлами; 1080
автоматически выбирает доступный узел для Telegram. URL аудиопотока и сам поток
проходят через один и тот же закреплённый узел; сбой извлечения проверяет резерв.

После `docker compose up -d --wait` настройте бота:

```sh
docker compose exec -T api python - < infra/configure_telegram.py
python3 infra/smoke.py https://your-domain.example --audio-enabled
```

Если входящие соединения от Telegram недоступны, long polling получает команды
через VPN в фоновой задаче API. Отдельный процесс бота не нужен. Не включайте
одновременно несколько API-воркеров. При выключенном polling используется вебхук.
Код Яндекса
подтверждается в браузере пользователя; в Telegram используется `openLink`.
Подписка и токен бота находятся только в приватных файлах; в Git их добавлять нельзя.

Если хостинг уже предоставляет TLS/reverse proxy (например, Docker-панель),
можно оставить Caddy в HTTP-режиме `SITE_ADDRESS=:80`, направить внешний proxy на
его HTTP-порт и задать публичный HTTPS origin в `CORS_ORIGINS`. Внешний proxy
должен пропускать WebSocket Upgrade. Не публикуйте порт API напрямую.

Справка: [Compose и healthcheck](https://docs.docker.com/compose/how-tos/startup-order/),
[Caddy reverse_proxy/WebSocket](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy),
[uv в Docker](https://docs.astral.sh/uv/guides/integration/docker/),
[GitHub Container Registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).
