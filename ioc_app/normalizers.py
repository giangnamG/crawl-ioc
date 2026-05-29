import re
from urllib.parse import urlsplit, urlunsplit


TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
}


def normalize_url(value: str) -> str | None:
    if not value:
        return None

    value = value.strip().strip(".,;)'\"]")
    if value.startswith("//"):
        value = "https:" + value

    parts = urlsplit(value)
    if not parts.scheme:
        value = "https://" + value
        parts = urlsplit(value)

    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None

    host = parts.hostname.lower() if parts.hostname else ""
    if not host or host in {"localhost"}:
        return None

    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None

    port = parts.port
    netloc = host
    if port and not (parts.scheme == "http" and port == 80) and not (
        parts.scheme == "https" and port == 443
    ):
        netloc = f"{host}:{port}"

    query_items = []
    for item in parts.query.split("&"):
        if not item:
            continue
        key = item.split("=", 1)[0]
        key_l = key.lower()
        if key_l.startswith("utm_") or key_l in TRACKING_PARAMS:
            continue
        query_items.append(item)

    query = "&".join(query_items)
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def get_domain(value: str) -> str | None:
    parts = urlsplit(value if "://" in value else f"https://{value}")
    host = parts.hostname
    if not host:
        return None
    host = host.strip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def normalize_domain(value: str) -> str | None:
    value = (value or "").strip().strip(".,;)'\"]").lower()
    value = re.sub(r"^https?://", "", value)
    value = value.split("/")[0].split(":")[0].strip(".")
    if not value or "." not in value:
        return None
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def normalize_email(value: str) -> str | None:
    value = (value or "").strip().strip(".,;)'\"]")
    if "@" not in value:
        return None
    local, domain = value.rsplit("@", 1)
    domain_norm = normalize_domain(domain)
    if not local or not domain_norm:
        return None
    return f"{local}@{domain_norm}"


def normalize_phone_vn(value: str) -> str | None:
    digits = re.sub(r"\D+", "", value or "")
    if digits.startswith("84") and len(digits) == 11:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+84" + digits[1:]
    return digits if len(digits) >= 8 else None


def normalize_hash(value: str) -> str | None:
    value = (value or "").strip().lower()
    return value if re.fullmatch(r"[a-f0-9]{32}|[a-f0-9]{40}|[a-f0-9]{64}|[a-f0-9]{128}", value) else None


def normalize_address(value: str) -> str | None:
    value = re.sub(r"\s+", " ", (value or "").strip(" :-\t\r\n"))
    return value if len(value) >= 8 else None


def normalize_by_rule(value: str, normalizer: str, ioc_type: str) -> str | None:
    normalizer = normalizer or "default"
    if normalizer == "none":
        return value
    if normalizer == "lowercase":
        return (value or "").strip().lower()
    if normalizer == "url" or ioc_type == "url":
        return normalize_url(value)
    if normalizer == "domain" or ioc_type == "domain":
        return normalize_domain(value)
    if normalizer == "email" or ioc_type == "email":
        return normalize_email(value)
    if normalizer == "phone_vn":
        return normalize_phone_vn(value)
    if normalizer == "hash" or ioc_type.startswith("hash_"):
        return normalize_hash(value)
    if normalizer == "address" or ioc_type == "address":
        return normalize_address(value)
    return (value or "").strip()
