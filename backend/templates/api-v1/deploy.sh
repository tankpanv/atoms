#!/bin/sh
set -eu
cd "$(dirname "$0")"
# env.connector is downloaded by the project owner; Compose reads it directly,
# without evaluating credentials as shell code or copying it into the image.
set -- -f compose.yaml
if [ -f env.connector ]; then
  set -- --env-file env.connector "$@"
  if [ -n "${APP_DATABASE_NETWORK:-}" ] || grep -q '^APP_DATABASE_NETWORK=.' env.connector; then
    set -- "$@" -f compose.connector.yaml
  fi
elif [ -n "${APP_DATABASE_NETWORK:-}" ]; then
  set -- "$@" -f compose.connector.yaml
fi
docker compose "$@" up --build -d
printf 'Application: http://%s:%s\n' "${APP_BIND_ADDRESS:-127.0.0.1}" "${APP_PORT:-23871}"
