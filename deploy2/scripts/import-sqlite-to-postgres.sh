#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_dir="$(cd -- "$script_dir/.." && pwd)"
env_file="${ENV_FILE:-$deploy_dir/app.env}"
compose_file="${COMPOSE_FILE:-$deploy_dir/docker-compose.app.yml}"
sqlite_path=""
replace=0
build=1

usage() {
    cat <<'EOF'
Usage:
  ./scripts/import-sqlite-to-postgres.sh --sqlite-path /path/to/ioc_investigator.sqlite3 --replace [options]

Options:
  --env-file <path>  App env file with POSTGRES_HOST/PORT/DB/USER/PASSWORD.
  --sqlite-path <p>  Old SQLite database file.
  --replace          Replace data in the target PostgreSQL app tables before import.
  --no-build         Do not build the backend image before running the importer.

Run this while the production app is stopped or before the first app deploy.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-file)
            env_file="${2:-}"
            shift
            ;;
        --sqlite-path)
            sqlite_path="${2:-}"
            shift
            ;;
        --replace)
            replace=1
            ;;
        --no-build)
            build=0
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

if [[ -z "$sqlite_path" || "$replace" -ne 1 ]]; then
    usage >&2
    exit 2
fi

if [[ ! -f "$sqlite_path" ]]; then
    echo "SQLite file not found: $sqlite_path" >&2
    exit 1
fi

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 1
    fi
}

require_command docker
docker compose version >/dev/null

env_file="$(realpath "$env_file")"
sqlite_abs="$(realpath "$sqlite_path")"
sqlite_dir="$(dirname "$sqlite_abs")"
sqlite_base="$(basename "$sqlite_abs")"

"$script_dir/validate-env.sh" app "$env_file"

cd "$deploy_dir"
compose=(docker compose --env-file "$env_file" -f "$compose_file")

if [[ "$build" -eq 1 ]]; then
    APP_ENV_FILE="$env_file" "${compose[@]}" build backend
fi

APP_ENV_FILE="$env_file" "${compose[@]}" run --rm --no-deps \
    -v "$sqlite_dir:/import" \
    -v "$script_dir/import_sqlite_to_postgres.py:/app/import_sqlite_to_postgres.py:ro" \
    backend \
    python /app/import_sqlite_to_postgres.py \
        --sqlite-path "/import/$sqlite_base" \
        --replace
