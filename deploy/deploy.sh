#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"

ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.yml}"
POSTGRES_DATA_DIR="${POSTGRES_DATA_DIR:-/opt/url-hunter/postgresql/data}"

PULL=1
BUILD=1
NO_CACHE=0
SHOW_LOGS=0
SERVICES=()

usage() {
    cat <<'EOF'
Usage:
  ./deploy.sh [options] [service...]

Options:
  --no-pull    Skip git pull.
  --no-build   Run containers without rebuilding images.
  --no-cache   Rebuild images without Docker cache.
  --logs       Show recent backend/nginx/postgres logs after deploy.
  -h, --help   Show this help.

Examples:
  ./deploy.sh
  ./deploy.sh --no-cache backend
  ./deploy.sh --no-pull --logs
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-pull)
            PULL=0
            ;;
        --no-build)
            BUILD=0
            ;;
        --no-cache)
            NO_CACHE=1
            ;;
        --logs)
            SHOW_LOGS=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            while [[ $# -gt 0 ]]; do
                SERVICES+=("$1")
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
            SERVICES+=("$1")
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

ensure_postgres_data_dir() {
    if mkdir -p "$POSTGRES_DATA_DIR" 2>/dev/null; then
        return
    fi
    if command -v sudo >/dev/null 2>&1; then
        sudo mkdir -p "$POSTGRES_DATA_DIR"
        return
    fi
    echo "Cannot create PostgreSQL data dir: $POSTGRES_DATA_DIR" >&2
    exit 1
}

require_command docker
docker compose version >/dev/null

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Missing env file: $ENV_FILE" >&2
    exit 1
fi

cd "$PROJECT_DIR"

if [[ "$PULL" -eq 1 ]]; then
    require_command git
    echo "Pulling latest code..."
    git pull --ff-only
fi

echo "Ensuring PostgreSQL data dir: $POSTGRES_DATA_DIR"
ensure_postgres_data_dir

COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")

if [[ "$BUILD" -eq 1 && "$NO_CACHE" -eq 1 ]]; then
    echo "Building images without cache..."
    "${COMPOSE[@]}" build --no-cache "${SERVICES[@]}"
fi

UP_ARGS=(up -d)
if [[ "$BUILD" -eq 1 && "$NO_CACHE" -eq 0 ]]; then
    UP_ARGS+=(--build)
fi

echo "Starting production stack..."
"${COMPOSE[@]}" "${UP_ARGS[@]}" "${SERVICES[@]}"

echo "Current containers:"
"${COMPOSE[@]}" ps

if [[ "$SHOW_LOGS" -eq 1 ]]; then
    echo "Recent logs:"
    "${COMPOSE[@]}" logs --tail=80 backend nginx postgres
fi
