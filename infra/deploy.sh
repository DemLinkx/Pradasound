#!/bin/sh
set -eu

prada_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$prada_root"

if [ ! -f .env ]; then
    printf '%s\n' 'Создайте .env из .env.example и настройте домен и CORS_ORIGINS.' >&2
    exit 1
fi

docker compose config --quiet
case "${1:-build}" in
    build) docker compose build --pull ;;
    pull) docker compose pull --policy always ;;
    *) printf '%s\n' 'Использование: ./infra/deploy.sh [build|pull]' >&2; exit 1 ;;
esac

docker compose up -d --no-build --wait --wait-timeout 120
docker compose ps
