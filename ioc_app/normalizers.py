import re
import ipaddress
from urllib.parse import urlsplit, urlunsplit


TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
}

VALID_TLDS = {
    "ac", "academy", "accountants", "ae", "ai", "app", "asia", "at", "au",
    "be", "bet", "biz", "br", "ca", "casino", "cc", "ch", "club", "co",
    "com", "condos", "de", "dev", "digital", "dk", "edu", "es", "eu",
    "fi", "fr", "fun", "games", "gdn", "gg", "gov", "group", "help",
    "host", "icu", "id", "in", "info", "ink", "io", "ir", "it", "jp", "kr",
    "la", "link", "live", "lol", "lu", "me", "miami", "mobi", "net",
    "nl", "online", "org", "pizza", "pro", "promo", "ren", "ru", "se",
    "shop", "site", "space", "store", "support", "top", "tv", "uk", "us",
    "vip", "vn", "wales", "win", "ws", "xyz", "yachts",
}

INVALID_DOMAIN_TLDS = {
    "body",
    "constructor",
    "contentwindow",
    "document",
    "documentelement",
    "hash",
    "is",
    "length",
    "limit",
    "margin",
    "max",
    "min",
    "offset",
    "over",
    "ownerdocument",
    "prophooks",
    "scrollleft",
    "slice",
    "split",
    "style",
    "tolowercase",
}

CODELIKE_DOMAIN_PREFIX_LABELS = {
    "document",
    "jquery",
    "location",
    "math",
    "prototype",
    "this",
    "window",
}

DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
EMAIL_RE = re.compile(
    r"^[A-Z0-9](?:[A-Z0-9._%+-]{0,62}[A-Z0-9])?@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)
CODELIKE_ADDRESS_RE = re.compile(
    r"[{}();=<>]|\b(?:window|function|jquery|document|location\.|addEventListener|parseInt|const|let)\b",
    re.IGNORECASE,
)
CONTACT_LABEL_RE = re.compile(
    r"\b(?:email|e-mail|mail|sdt|sđt|phone|tel|telephone|hotline|website|web|social)\b\s*:?",
    re.IGNORECASE,
)
ADDRESS_SIGNAL_RE = re.compile(
    r"\b(?:đ\.|ngõ|ngo|ngh\.|phường|phuong|p\.|quận|quan|q\.|huyện|huyen|"
    r"tp\.?|thành phố|thanh pho|hồ chí minh|ho chi minh|hà nội|ha noi|việt nam|viet nam|"
    r"street|ward|district|city)\b",
    re.IGNORECASE,
)


def normalize_url(value: str) -> str | None:
    if not value:
        return None

    value = value.strip().strip(".,;:)]}'\"")
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
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_multicast:
            return None
    except ValueError:
        host = normalize_domain(host)
        if not host:
            return None
    except UnicodeError:
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
    value = (value or "").strip().strip(".,;:)]}'\"").lower()
    value = re.sub(r"^https?://", "", value)
    value = value.split("/")[0].split(":")[0].strip(".")
    if not value or "." not in value:
        return None
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return value if is_valid_domain(value) else None


def is_valid_domain(value: str) -> bool:
    value = (value or "").strip(".").lower()
    labels = value.split(".")
    if len(labels) < 2 or len(value) > 253:
        return False
    if any(not DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        return False
    tld = labels[-1]
    if tld in INVALID_DOMAIN_TLDS or tld not in VALID_TLDS:
        return False
    if labels[0] in CODELIKE_DOMAIN_PREFIX_LABELS:
        return False
    if len(labels) == 2 and len(labels[0]) == 1:
        return False
    return True


def normalize_email(value: str) -> str | None:
    value = (value or "").strip().strip(".,;:)]}'\"")
    if not EMAIL_RE.fullmatch(value):
        return None
    local, domain = value.rsplit("@", 1)
    if ".." in local or local.startswith(".") or local.endswith("."):
        return None
    domain_norm = normalize_domain(domain)
    if not local or not domain_norm:
        return None
    return f"{local}@{domain_norm}"


def normalize_phone_vn(value: str) -> str | None:
    digits = re.sub(r"\D+", "", value or "")
    if digits.startswith("84") and len(digits) == 11 and digits[2] in "35789":
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10 and digits[1] in "35789":
        return "+84" + digits[1:]
    return None


def normalize_hash(value: str) -> str | None:
    value = (value or "").strip().lower()
    return value if re.fullmatch(r"[a-f0-9]{32}|[a-f0-9]{40}|[a-f0-9]{64}|[a-f0-9]{128}", value) else None


def normalize_address(value: str) -> str | None:
    value = re.sub(r"\s+", " ", (value or "").strip(" :-\t\r\n"))
    value = CONTACT_LABEL_RE.split(value, maxsplit=1)[0].strip(" ,;-")
    if len(value) < 8 or len(value) > 180:
        return None
    if not re.search(r"\d", value):
        return None
    if CODELIKE_ADDRESS_RE.search(value):
        return None
    if not ADDRESS_SIGNAL_RE.search(value):
        return None
    return value


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
