from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass
from sqlite3 import Row

from .normalizers import is_probable_phone_vn_evidence, normalize_by_rule


MAX_INPUT_CHARS = 500_000
MAX_MATCHES_PER_RULE = 500


@dataclass
class ExtractedIOC:
    type: str
    raw: str
    norm: str
    evidence: str
    rule_id: int


def compile_flags(flags: str | None) -> int:
    out = 0
    flags = flags or ""
    if "i" in flags:
        out |= re.IGNORECASE
    if "m" in flags:
        out |= re.MULTILINE
    if "s" in flags:
        out |= re.DOTALL
    return out


def validate_rule(pattern: str, flags: str, value_group: int, sample_text: str = "") -> tuple[bool, str | None]:
    if not pattern or not pattern.strip():
        return False, "Pattern is required."
    if len(pattern) > 2000:
        return False, "Pattern is too long; keep it under 2,000 characters."
    if "\n" in flags or len(flags or "") > 8:
        return False, "Regex flags are invalid."
    try:
        compiled = re.compile(pattern, compile_flags(flags))
    except re.error as exc:
        return False, f"Regex compile error: {exc}"
    if value_group < 0 or value_group > compiled.groups:
        return False, f"value_group {value_group} does not exist. Pattern has {compiled.groups} capture groups."
    if sample_text:
        try:
            list(compiled.finditer(sample_text[:50_000]))
        except re.error as exc:
            return False, f"Regex test error: {exc}"
    return True, None


def extract_iocs_by_rules(
    extraction_input: dict[str, object], rules: list[Row], dedupe: bool = True
) -> list[ExtractedIOC]:
    found: list[ExtractedIOC] = []

    for rule in sorted(rules, key=lambda item: item["priority"]):
        source_text = build_input_by_scope(extraction_input, rule["input_scope"])
        if not source_text:
            continue
        source_text = source_text[:MAX_INPUT_CHARS]

        try:
            pattern = re.compile(rule["pattern"], compile_flags(rule["flags"]))
            exclude_pattern = (
                re.compile(rule["exclude_pattern"], compile_flags(rule["flags"]))
                if rule["exclude_pattern"]
                else None
            )
        except re.error:
            continue

        match_count = 0
        for match in pattern.finditer(source_text):
            match_count += 1
            if match_count > MAX_MATCHES_PER_RULE:
                break

            raw_value = match.group(rule["value_group"])
            if not raw_value:
                continue

            if exclude_pattern and exclude_pattern.search(raw_value):
                continue

            norm_value = normalize_by_rule(raw_value, rule["normalizer"], rule["ioc_type"])
            if not norm_value:
                continue

            evidence = get_context(source_text, match.start(), match.end())
            if rule["ioc_type"] == "phone" and not is_probable_phone_vn_evidence(
                norm_value,
                evidence,
                raw_value,
            ):
                continue

            found.append(
                ExtractedIOC(
                    type=rule["ioc_type"],
                    raw=raw_value.strip(),
                    norm=norm_value,
                    evidence=evidence,
                    rule_id=rule["id"],
                )
            )

    return dedupe_iocs(found) if dedupe else found


def test_rule(rule: dict[str, object], sample_text: str) -> tuple[list[ExtractedIOC], list[str]]:
    warnings: list[str] = []
    ok, error = validate_rule(
        str(rule.get("pattern") or ""),
        str(rule.get("flags") or ""),
        int(rule.get("value_group") or 0),
        sample_text,
    )
    if not ok:
        return [], [error or "Invalid rule."]

    class RuleDict(dict):
        def __getitem__(self, key: str):
            return self.get(key)

    row = RuleDict(
        id=int(rule.get("id") or 0),
        ioc_type=rule.get("ioc_type") or "unknown",
        pattern=rule.get("pattern") or "",
        flags=rule.get("flags") or "",
        value_group=int(rule.get("value_group") or 0),
        input_scope=rule.get("input_scope") or "text",
        exclude_pattern=rule.get("exclude_pattern") or None,
        normalizer=rule.get("normalizer") or "default",
        priority=int(rule.get("priority") or 100),
    )
    matches = extract_iocs_by_rules({"text": sample_text, "all": sample_text}, [row], dedupe=False)[:50]
    if len(matches) >= 50:
        warnings.append("Only the first 50 matches are displayed.")
    return matches, warnings


def build_input_by_scope(extraction_input: dict[str, object], scope: str) -> str:
    scope = scope or "text"
    if scope == "all":
        parts = []
        for key in ("final_url", "redirects", "links", "text", "html"):
            value = extraction_input.get(key)
            if isinstance(value, list):
                parts.append(expand_encoded_text("\n".join(str(item) for item in value)))
            elif value:
                parts.append(expand_encoded_text(str(value)))
        return "\n".join(parts)

    value = extraction_input.get(scope)
    if isinstance(value, list):
        return expand_encoded_text("\n".join(str(item) for item in value))
    return expand_encoded_text(str(value or ""))


def expand_encoded_text(text: str) -> str:
    variants = [text]
    unescaped = html.unescape(text)
    if unescaped != text:
        variants.append(unescaped)
    unquoted = urllib.parse.unquote(unescaped)
    if unquoted != unescaped and unquoted != text:
        variants.append(unquoted)
    return "\n".join(variants)


def get_context(text: str, start: int, end: int, size: int = 80) -> str:
    left = max(0, start - size)
    right = min(len(text), end + size)
    return " ".join(text[left:right].split())


def dedupe_iocs(items: list[ExtractedIOC]) -> list[ExtractedIOC]:
    seen: set[tuple[str, str, int]] = set()
    output: list[ExtractedIOC] = []
    for item in items:
        key = (item.type, item.norm, item.rule_id)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output
