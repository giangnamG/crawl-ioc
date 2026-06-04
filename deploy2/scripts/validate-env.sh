#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage:
  ./scripts/validate-env.sh db  ./db.env
  ./scripts/validate-env.sh app ./app.env
EOF
}

mode="${1:-}"
env_file="${2:-}"

if [[ -z "$mode" || -z "$env_file" || ! "$mode" =~ ^(db|app)$ ]]; then
    usage >&2
    exit 2
fi

if [[ ! -f "$env_file" ]]; then
    echo "Missing env file: $env_file" >&2
    exit 1
fi

load_env_file() {
    local file="$1"
    local line key value
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
        if [[ "$line" != *=* ]]; then
            echo "Invalid env line without '=': $line" >&2
            exit 1
        fi
        key="${line%%=*}"
        value="${line#*=}"
        if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
            echo "Invalid env key: $key" >&2
            exit 1
        fi
        if [[ "$value" == \"*\" && "$value" == *\" && ${#value} -ge 2 ]]; then
            value="${value:1:${#value}-2}"
        elif [[ "$value" == \'*\' && "$value" == *\' && ${#value} -ge 2 ]]; then
            value="${value:1:${#value}-2}"
        fi
        export "$key=$value"
    done < "$file"
}

value_of() {
    local name="$1"
    printf '%s' "${!name:-}"
}

require_var() {
    local name="$1"
    if [[ -z "$(value_of "$name")" ]]; then
        echo "$name is required" >&2
        exit 1
    fi
}

reject_placeholder() {
    local name="$1"
    local value lower
    value="$(value_of "$name")"
    lower="${value,,}"
    if [[ "$lower" == *replace* || "$lower" == *change-me* || "$lower" == *change_this* || "$lower" == *password* ]]; then
        echo "$name still looks like a placeholder" >&2
        exit 1
    fi
}

validate_identifier() {
    local name="$1"
    local value
    value="$(value_of "$name")"
    if [[ ! "$value" =~ ^[A-Za-z_][A-Za-z0-9_]{2,62}$ ]]; then
        echo "$name must match ^[A-Za-z_][A-Za-z0-9_]{2,62}$" >&2
        exit 1
    fi
}

validate_port() {
    local name="$1"
    local value
    value="$(value_of "$name")"
    if [[ ! "$value" =~ ^[0-9]{1,5}$ || "$value" -lt 1 || "$value" -gt 65535 ]]; then
        echo "$name must be a TCP port from 1 to 65535" >&2
        exit 1
    fi
}

validate_strong_secret() {
    local name="$1"
    local min_length="${2:-32}"
    local value
    value="$(value_of "$name")"
    reject_placeholder "$name"
    if (( ${#value} < min_length )); then
        echo "$name must be at least $min_length characters" >&2
        exit 1
    fi
    [[ "$value" =~ [A-Z] ]] || { echo "$name must contain uppercase letters" >&2; exit 1; }
    [[ "$value" =~ [a-z] ]] || { echo "$name must contain lowercase letters" >&2; exit 1; }
    [[ "$value" =~ [0-9] ]] || { echo "$name must contain digits" >&2; exit 1; }
    [[ "$value" =~ [^A-Za-z0-9] ]] || { echo "$name must contain symbols" >&2; exit 1; }
}

load_env_file "$env_file"

case "$mode" in
    db)
        for name in POSTGRES_DB POSTGRES_ADMIN_USER POSTGRES_ADMIN_PASSWORD POSTGRES_APP_USER POSTGRES_APP_PASSWORD POSTGRES_PORT POSTGRES_BIND_ADDRESS POSTGRES_DATA_DIR; do
            require_var "$name"
        done
        validate_identifier POSTGRES_DB
        validate_identifier POSTGRES_ADMIN_USER
        validate_identifier POSTGRES_APP_USER
        validate_strong_secret POSTGRES_ADMIN_PASSWORD 32
        validate_strong_secret POSTGRES_APP_PASSWORD 32
        validate_port POSTGRES_PORT
        ;;
    app)
        for name in SECRET_KEY POSTGRES_HOST POSTGRES_PORT POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD NGINX_BASIC_AUTH_USER NGINX_BASIC_AUTH_PASSWORD HTTP_PORT BACKEND_DATA_DIR; do
            require_var "$name"
        done
        if [[ "$(value_of POSTGRES_HOST)" == "10.0.0.20" || "$(value_of POSTGRES_HOST)" == *CHANGE_ME* ]]; then
            echo "POSTGRES_HOST must be the real DB VPS private/public IP or DNS name" >&2
            exit 1
        fi
        validate_identifier POSTGRES_DB
        validate_identifier POSTGRES_USER
        validate_strong_secret POSTGRES_PASSWORD 32
        validate_strong_secret SECRET_KEY 32
        validate_strong_secret NGINX_BASIC_AUTH_PASSWORD 16
        validate_port POSTGRES_PORT
        validate_port HTTP_PORT
        ;;
esac

echo "$mode env is valid: $env_file"

