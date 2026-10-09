#!/usr/bin/env bash
# Bootstrap the host, then hand deployment to the safe (non-shell) env reader.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ "${1:-}" == -h || "${1:-}" == --help ]]; then
  cat <<'HELP'
Usage: bash deploy.sh [start|restart|stop|status|check] [options]
  --host HOST          Set browser URLs to http://HOST:<configured ports>
  --env-file FILE      Use a separate env file (default: .env)
  --no-build           Reuse existing images; still restart and check services
  --timeout SECONDS    Startup health-check timeout (default: 600)
start/restart: build first, stop this stack and its project workers, start, verify.
check: verify an already running stack. stop/status do not install host packages.
HELP
  exit 0
fi
admin=()
if [[ $EUID -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then admin=(sudo); fi
fi
install_packages() {
  if [[ $EUID -ne 0 && ${#admin[@]} -eq 0 ]]; then
    echo '需要安装系统依赖，请以 root 执行或先安装 sudo。' >&2; exit 1
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    echo '自动安装仅支持 Ubuntu/Debian；其他 Linux 请先安装 Python 3、curl、Docker Engine 和 Compose v2。' >&2; exit 1
  fi
  "${admin[@]}" apt-get update
  "${admin[@]}" apt-get install -y "$@"
}
mode="${1:-start}"
if [[ "$mode" == stop || "$mode" == status || "$mode" == check ]]; then
  if ! command -v python3 >/dev/null || ! command -v docker >/dev/null; then
    echo '尚未安装，请先运行 bash deploy.sh。' >&2; exit 1
  fi
else
  missing=()
  command -v python3 >/dev/null || missing+=(python3)
  command -v curl >/dev/null || missing+=(curl ca-certificates)
  if ((${#missing[@]})); then install_packages "${missing[@]}"; fi
  if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1 || ! docker buildx version >/dev/null 2>&1; then
    # Official signed Docker apt repository; never execute a downloaded shell script.
    [[ -r /etc/os-release ]] || { echo '无法识别系统，请手动安装 Docker。' >&2; exit 1; }
    # shellcheck source=/dev/null
    source /etc/os-release
    case "$ID" in ubuntu|debian) ;; *) echo '请先按 README 安装 Docker Engine / Compose v2。' >&2; exit 1;; esac
    distro_codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
    [[ -n "$distro_codename" ]] || { echo '无法识别发行版代号。' >&2; exit 1; }
    install_packages ca-certificates curl
    docker_key_file=''
    # Preserve an existing official repository/key configuration (avoid Signed-By conflicts).
    if ! grep -Rqs "download.docker.com/linux/$ID" /etc/apt/sources.list /etc/apt/sources.list.d; then
      "${admin[@]}" install -m 0755 -d /etc/apt/keyrings
      docker_key_file=$(mktemp)
      trap 'rm -f -- "$docker_key_file"' EXIT
      curl --retry 3 --connect-timeout 20 -fsSL "https://download.docker.com/linux/$ID/gpg" -o "$docker_key_file"
      "${admin[@]}" install -m 0644 "$docker_key_file" /etc/apt/keyrings/docker.asc
      printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$ID" "$distro_codename" "$(dpkg --print-architecture)" | "${admin[@]}" tee /etc/apt/sources.list.d/docker.sources >/dev/null
    fi
    if command -v docker >/dev/null; then
      install_packages docker-compose-plugin docker-buildx-plugin
    else
      install_packages docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    fi
    if [[ -n "${docker_key_file:-}" ]]; then rm -f -- "$docker_key_file"; trap - EXIT; fi
  fi
  if ! docker info >/dev/null 2>&1 && ! "${admin[@]}" docker info >/dev/null 2>&1; then
    if command -v systemctl >/dev/null; then "${admin[@]}" systemctl enable --now docker; fi
  fi
fi
exec python3 scripts/deploy.py "$@"
