#!/bin/sh
set -eu

require_var() {
    name="$1"
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then
        echo "$name is required" >&2
        exit 1
    fi
}

validate_identifier() {
    name="$1"
    value="$2"
    if ! printf '%s' "$value" | grep -Eq '^[A-Za-z_][A-Za-z0-9_]{2,62}$'; then
        echo "$name must match ^[A-Za-z_][A-Za-z0-9_]{2,62}$" >&2
        exit 1
    fi
}

validate_strong_password() {
    name="$1"
    value="$2"
    length="$(printf '%s' "$value" | wc -c | tr -d ' ')"
    if [ "$length" -lt 32 ]; then
        echo "$name must be at least 32 characters" >&2
        exit 1
    fi
    printf '%s' "$value" | grep -Eq '[A-Z]' || { echo "$name must contain uppercase letters" >&2; exit 1; }
    printf '%s' "$value" | grep -Eq '[a-z]' || { echo "$name must contain lowercase letters" >&2; exit 1; }
    printf '%s' "$value" | grep -Eq '[0-9]' || { echo "$name must contain digits" >&2; exit 1; }
    printf '%s' "$value" | grep -Eq '[^A-Za-z0-9]' || { echo "$name must contain symbols" >&2; exit 1; }
}

require_var POSTGRES_DB
require_var POSTGRES_APP_USER
require_var POSTGRES_APP_PASSWORD

validate_identifier POSTGRES_DB "$POSTGRES_DB"
validate_identifier POSTGRES_APP_USER "$POSTGRES_APP_USER"
validate_strong_password POSTGRES_APP_PASSWORD "$POSTGRES_APP_PASSWORD"

app_password_sql="$(printf '%s' "$POSTGRES_APP_PASSWORD" | sed "s/'/''/g")"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${POSTGRES_APP_USER}') THEN
    CREATE ROLE "${POSTGRES_APP_USER}" LOGIN PASSWORD '${app_password_sql}';
  ELSE
    ALTER ROLE "${POSTGRES_APP_USER}" WITH LOGIN PASSWORD '${app_password_sql}';
  END IF;
END
\$\$;

ALTER DATABASE "${POSTGRES_DB}" OWNER TO "${POSTGRES_APP_USER}";
ALTER SCHEMA public OWNER TO "${POSTGRES_APP_USER}";
GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO "${POSTGRES_APP_USER}";
GRANT USAGE, CREATE ON SCHEMA public TO "${POSTGRES_APP_USER}";
EOSQL

