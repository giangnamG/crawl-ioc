#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_dir="$(cd -- "$script_dir/.." && pwd)"

postgres_host="${POSTGRES_HOST:-}"
postgres_port="${POSTGRES_PORT:-5432}"
postgres_bind_address="${POSTGRES_BIND_ADDRESS:-0.0.0.0}"
http_port="${HTTP_PORT:-80}"
output_dir="$deploy_dir"
force=0

usage() {
    cat <<'EOF'
Usage:
  ./scripts/generate-secrets.sh --postgres-host <db-vps-ip-or-dns> [options]

Options:
  --postgres-host <value>       IP/DNS that backend uses to reach PostgreSQL.
  --postgres-port <port>        PostgreSQL host port. Default: 5432.
  --postgres-bind-address <ip>  DB VPS bind address. Default: 0.0.0.0.
  --http-port <port>            Nginx HTTP port on app VPS. Default: 80.
  --output-dir <dir>            Directory to write db.env and app.env. Default: deploy2.
  --force                       Overwrite existing db.env/app.env.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --postgres-host)
            postgres_host="${2:-}"
            shift
            ;;
        --postgres-port)
            postgres_port="${2:-}"
            shift
            ;;
        --postgres-bind-address)
            postgres_bind_address="${2:-}"
            shift
            ;;
        --http-port)
            http_port="${2:-}"
            shift
            ;;
        --output-dir)
            output_dir="${2:-}"
            shift
            ;;
        --force)
            force=1
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

if [[ -z "$postgres_host" ]]; then
    echo "--postgres-host is required" >&2
    usage >&2
    exit 2
fi

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 1
    fi
}

strong_secret() {
    local random_part
    random_part="$(openssl rand -base64 64 | tr '+/' '@_' | tr -d '=\n\r' | cut -c1-44)"
    printf '%sAa1@' "$random_part"
}

random_user() {
    local prefix="$1"
    printf '%s_%s' "$prefix" "$(openssl rand -hex 5)"
}

write_file() {
    local path="$1"
    local content="$2"
    if [[ -f "$path" && "$force" -ne 1 ]]; then
        echo "Refusing to overwrite existing file: $path" >&2
        echo "Use --force if you intentionally want to regenerate secrets." >&2
        exit 1
    fi
    umask 077
    printf '%s\n' "$content" > "$path"
}

require_command openssl
mkdir -p "$output_dir"

postgres_db="ioc_investigator"
postgres_admin_user="$(random_user pg_admin)"
postgres_admin_password="$(strong_secret)"
postgres_app_user="$(random_user ioc_app)"
postgres_app_password="$(strong_secret)"
secret_key="$(strong_secret)$(strong_secret)"
basic_auth_password="$(strong_secret)"

db_env="$output_dir/db.env"
app_env="$output_dir/app.env"

write_file "$db_env" "POSTGRES_DB=$postgres_db
POSTGRES_ADMIN_USER=$postgres_admin_user
POSTGRES_ADMIN_PASSWORD=$postgres_admin_password
POSTGRES_APP_USER=$postgres_app_user
POSTGRES_APP_PASSWORD=$postgres_app_password
POSTGRES_PORT=$postgres_port
POSTGRES_BIND_ADDRESS=$postgres_bind_address
POSTGRES_DATA_DIR=/opt/88i/postgresql/data"

write_file "$app_env" "APP_ENV_FILE=app.env
SECRET_KEY=$secret_key

POSTGRES_HOST=$postgres_host
POSTGRES_PORT=$postgres_port
POSTGRES_DB=$postgres_db
POSTGRES_USER=$postgres_app_user
POSTGRES_PASSWORD=$postgres_app_password

NGINX_BASIC_AUTH_USER=admin
NGINX_BASIC_AUTH_PASSWORD=$basic_auth_password
HTTP_PORT=$http_port
BACKEND_DATA_DIR=/opt/88i/backend/data

AUTO_WORKER_ENABLED=true
WORKER_POLL_SECONDS=3
URL_CRAWL_CONCURRENCY=30
URL_CRAWL_WORKER_THREADS=4
WORKER_HEARTBEAT_SECONDS=5
WORKER_SLOT_STALE_SECONDS=900
WORKER_SLOT_MAX_SECONDS=1800
WORKER_MAINTAINER_SECONDS=10

BROWSER_PROVIDER=cloak
BROWSER_TIMEOUT=45
CLOAK_HEADLESS=true
CLOAK_HUMANIZE=true
CLOAK_HUMAN_PRESET=careful
CLOAK_STEALTH_ARGS=true
CLOAK_GEOIP=true
CLOAK_REQUIRE_PROXY_FOR_SEARCH=true
CLOAK_PROXY_PREFLIGHT=true
CLOAK_PROXY_PREFLIGHT_TIMEOUT=5
CLOAK_PROXY_PREFLIGHT_TTL_SECONDS=300
CLOAK_LOCALE=en-US
CLOAK_TIMEZONE=Asia/Saigon
CLOAK_DIRECT_FALLBACK=true
SEARCH_JOB_MAX_ATTEMPTS=3

SEARCH_MAX_PAGES=100
SEARCH_HARD_PAGE_LIMIT=100
SEARCH_FAST_MODE=false
SEARCH_TYPE_DELAY_MIN=2
SEARCH_TYPE_DELAY_MAX=5
SEARCH_PAGE_DELAY_MIN=2.5
SEARCH_PAGE_DELAY_MAX=6.0
GOOGLE_SEARCH_ENTRY=homepage
GOOGLE_NEXT_FALLBACK_DIRECT=true

HTTP_FETCH_MAX_BYTES=5000000
CRAWL_CLOAK_HTTP_FALLBACK=true"

"$script_dir/validate-env.sh" db "$db_env" >/dev/null
"$script_dir/validate-env.sh" app "$app_env" >/dev/null

echo "Created:"
echo "  $db_env"
echo "  $app_env"
echo
echo "Copy db.env to the PostgreSQL VPS and app.env to the backend/nginx VPS."
