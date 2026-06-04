#!/bin/sh
set -eu

auth_file="/etc/nginx/.htpasswd-app"

if [ -z "${NGINX_BASIC_AUTH_USER:-}" ] || [ -z "${NGINX_BASIC_AUTH_PASSWORD:-}" ]; then
    echo "NGINX_BASIC_AUTH_USER and NGINX_BASIC_AUTH_PASSWORD are required" >&2
    exit 1
fi

case "$NGINX_BASIC_AUTH_USER" in
    *:*)
        echo "NGINX_BASIC_AUTH_USER must not contain ':'" >&2
        exit 1
        ;;
esac

printf '%s\n' "$NGINX_BASIC_AUTH_PASSWORD" \
    | htpasswd -i -cB "$auth_file" "$NGINX_BASIC_AUTH_USER" >/dev/null

chown root:nginx "$auth_file"
chmod 640 "$auth_file"

