#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

if docker info >/dev/null 2>&1; then
  docker compose --profile local down
elif command -v sg >/dev/null 2>&1 && sg docker -c 'docker info' >/dev/null 2>&1; then
  sg docker -c 'docker compose --profile local down'
elif command -v sudo >/dev/null 2>&1; then
  sudo -v
  sudo docker compose --profile local down
else
  printf '%s\n' "No se puede conectar con Docker y sudo no está disponible." >&2
  exit 1
fi

printf '\nArepaGo se apagó. La base de datos y los incidentes se conservaron en el volumen postgres-data.\n'
