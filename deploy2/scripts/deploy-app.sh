#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_dir="$(cd -- "$script_dir/.." && pwd)"
env_file="${ENV_FILE:-$deploy_dir/app.env}"
compose_file="${COMPOSE_FILE:-$deploy_dir/docker-compose.app.yml}"
build=1
no_cache=0
show_logs=0
services=()

usage() {
    cat <<'EOF'
Usage:
  ./scripts/deploy-app.sh [options] [service...]

Options:
  --env-file <path>  Env file for the app VPS. Default: deploy2/app.env.
  --no-build         Start containers without rebuilding images.
  --no-cache         Rebuild images without Docker cache.
  --logs             Show recent backend/nginx logs after deploy.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-file)
            env_file="${2:-}"
            shift
            ;;
        --no-build)
            build=0
            ;;
        --no-cache)
            no_cache=1
            ;;
        --logs)
            show_logs=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            while [[ $# -gt 0 ]]; do
                services+=("$1")
                shift
            done
            break
            ;;
        -*)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            services+=("$1")
            ;;
    esac
    shift
done

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 1
    fi
}

env_value() {
    local key="$1"
    awk -F= -v key="$key" '$1 == key {print substr($0, length($1) + 2)}' "$env_file" | tail -n 1
}

ensure_dir() {
    local path="$1"
    if mkdir -p "$path" 2>/dev/null; then
        return
    fi
    if command -v sudo >/dev/null 2>&1; then
        sudo mkdir -p "$path"
        return
    fi
    echo "Cannot create directory: $path" >&2
    exit 1
}

require_command docker
docker compose version >/dev/null

env_file="$(realpath "$env_file")"
"$script_dir/validate-env.sh" app "$env_file"

backend_data_dir="$(env_value BACKEND_DATA_DIR)"
backend_data_dir="${backend_data_dir:-/opt/88i/backend/data}"
echo "Ensuring backend data dir: $backend_data_dir"
ensure_dir "$backend_data_dir"

cd "$deploy_dir"
compose=(docker compose --env-file "$env_file" -f "$compose_file")

if [[ "$build" -eq 1 && "$no_cache" -eq 1 ]]; then
    echo "Building images without cache..."
    APP_ENV_FILE="$env_file" "${compose[@]}" build --no-cache "${services[@]}"
fi

up_args=(up -d)
if [[ "$build" -eq 1 && "$no_cache" -eq 0 ]]; then
    up_args+=(--build)
fi

APP_ENV_FILE="$env_file" "${compose[@]}" "${up_args[@]}" "${services[@]}"
APP_ENV_FILE="$env_file" "${compose[@]}" ps

if [[ "$show_logs" -eq 1 ]]; then
    APP_ENV_FILE="$env_file" "${compose[@]}" logs --tail=80 backend nginx
fi

