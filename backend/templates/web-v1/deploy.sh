#!/bin/sh
set -eu
cd "$(dirname "$0")"
docker compose -f compose.yaml up --build -d
printf 'Application: http://%s:%s
' "${APP_BIND_ADDRESS:-127.0.0.1}" "${APP_PORT:-23871}"
