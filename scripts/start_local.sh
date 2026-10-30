#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

# Los secretos de Compose son archivos del host con permisos 600. Construimos
# la imagen con el UID/GID del usuario local para que el proceso no-root pueda
# leerlos sin abrirlos al resto del sistema.
APP_UID=$(id -u)
APP_GID=$(id -g)
export APP_UID APP_GID

if [ ! -f secrets/db_password.txt ] || [ ! -f secrets/admin_password.txt ]; then
  printf '%s\n' "Faltan secretos locales. Crea secrets/db_password.txt y secrets/admin_password.txt con contraseñas de al menos 12 caracteres." >&2
  exit 1
fi

if [ ! -f secrets/internal_auth_secret.txt ]; then
  python3 -c 'import secrets; print(secrets.token_hex(32))' > secrets/internal_auth_secret.txt
  chmod 600 secrets/internal_auth_secret.txt
  printf '%s\n' "Generado secrets/internal_auth_secret.txt para firmar la identidad interna."
fi

if docker info >/dev/null 2>&1; then
  docker compose --profile local up --build -d
elif command -v sg >/dev/null 2>&1 && sg docker -c 'docker info' >/dev/null 2>&1; then
  printf '%s\n' "Usando el grupo docker de la sesión mediante sg."
  sg docker -c 'docker compose --profile local up --build -d'
elif command -v sudo >/dev/null 2>&1; then
  printf '%s\n' "Docker requiere privilegios; sudo puede solicitar tu contraseña."
  sudo -v
  sudo docker compose --profile local up --build -d
else
  printf '%s\n' "No se puede conectar con Docker y sudo no está disponible." >&2
  exit 1
fi

printf '\nArepaGo está iniciando. Abre: http://localhost:8081\n'
printf 'Estado: docker compose --profile local ps\n'
printf 'Logs:   docker compose --profile local logs -f\n'
