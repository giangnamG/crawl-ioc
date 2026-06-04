#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_dir="$(cd -- "$script_dir/.." && pwd)"
env_file="${ENV_FILE:-$deploy_dir/db.env}"
compose_file="${COMPOSE_FILE:-$deploy_dir/docker-compose.db.yml}"
show_logs=0
pull=1

usage() {
    cat <<'EOF'
Usage:
  ./scripts/deploy-db.sh [options]

Options:
  --env-file <path>  Env file for the DB VPS. Default: deploy2/db.env.
  --no-pull          Skip pulling the postgres image.
  --logs             Show recent postgres logs after deploy.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-file)
            env_file="${2:-}"
            shift
            ;;
        --no-pull)
            pull=0
            ;;
        --logs)
            show_logs=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
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
"$script_dir/validate-env.sh" db "$env_file"

postgres_data_dir="$(env_value POSTGRES_DATA_DIR)"
postgres_data_dir="${postgres_data_dir:-/opt/88i/postgresql/data}"
echo "Ensuring PostgreSQL data dir: $postgres_data_dir"
ensure_dir "$postgres_data_dir"

cd "$deploy_dir"
compose=(docker compose --env-file "$env_file" -f "$compose_file")

if [[ "$pull" -eq 1 ]]; then
    "${compose[@]}" pull postgres
fi

"${compose[@]}" up -d
"${compose[@]}" ps

if [[ "$show_logs" -eq 1 ]]; then
    "${compose[@]}" logs --tail=80 postgres
fi

