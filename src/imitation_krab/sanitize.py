from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ASCII_HANDLE_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")
PROJECT_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{2,31}$")
OBJECT_ID_RE = re.compile(r"^(?:usr|ses|itm|clm)_[0-9a-f]{32}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,255}$")

_SEMANTIC_FLAGS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|override|forget)\b.{0,48}\b"
            r"(?:instruction|instructions|rules|prompt|policy|policies)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "credential_request",
        re.compile(
            r"\b(?:reveal|show|print|send|upload|exfiltrate|return)\b.{0,48}\b"
            r"(?:token|credential|credentials|secret|password|api[- ]?key)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "role_delimiter",
        re.compile(
            r"(?:<\|\s*(?:system|developer|assistant|user)\s*\|>|"
            r"\[(?:system|developer|assistant)\]|"
            r"\b(?:system|developer)\s+(?:message|prompt)\s*:)",
            re.IGNORECASE,
        ),
    ),
    ("external_url", re.compile(r"\bhttps?://", re.IGNORECASE)),
)


class ValidationError(ValueError):
    """Input failed deterministic validation."""


@dataclass(frozen=True)
class SanitizedText:
    text: str
    risk_flags: tuple[str, ...]


def validate_handle(value: object) -> str:
    if not isinstance(value, str) or not ASCII_HANDLE_RE.fullmatch(value):
        raise ValidationError("handle must match [a-z][a-z0-9_-]{2,31}")
    return value


def validate_project_key(value: object) -> str:
    if not isinstance(value, str) or not PROJECT_KEY_RE.fullmatch(value):
        raise ValidationError("project key must match [a-z][a-z0-9-]{2,31}")
    return value


def validate_object_id(value: object, prefix: str | None = None) -> str:
    if not isinstance(value, str) or not OBJECT_ID_RE.fullmatch(value):
        raise ValidationError("invalid object identifier")
    if prefix is not None and not value.startswith(prefix + "_"):
        raise ValidationError(f"expected a {prefix} identifier")
    return value


def validate_idempotency_key(value: object) -> str:
    if not isinstance(value, str) or not IDEMPOTENCY_KEY_RE.fullmatch(value):
        raise ValidationError("invalid Idempotency-Key")
    return value


def validate_external_id(value: object) -> str:
    """Validate an opaque, display-safe external issue or pull-request identifier."""
    if not isinstance(value, str) or not EXTERNAL_ID_RE.fullmatch(value):
        raise ValidationError(
            "external_id must be 1 to 256 restricted ASCII characters"
        )
    return value


def _is_noncharacter(codepoint: int) -> bool:
    return 0xFDD0 <= codepoint <= 0xFDEF or codepoint & 0xFFFF in (0xFFFE, 0xFFFF)


def sanitize_text(
    value: object,
    *,
    field: str,
    max_bytes: int,
    allow_empty: bool = False,
    max_lines: int = 200,
    max_line_chars: int = 4096,
) -> SanitizedText:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text")
    if not allow_empty and not value:
        raise ValidationError(f"{field} must not be empty")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise ValidationError(f"{field} must be valid UTF-8") from exc
    if len(encoded) > max_bytes:
        raise ValidationError(f"{field} exceeds {max_bytes} UTF-8 bytes")

    lines = value.split("\n")
    if len(lines) > max_lines:
        raise ValidationError(f"{field} exceeds {max_lines} lines")
    if any(len(line) > max_line_chars for line in lines):
        raise ValidationError(f"{field} contains an overlong line")

    for char in value:
        codepoint = ord(char)
        category = unicodedata.category(char)
        if char in ("\n", "\t"):
            continue
        if category in {"Cc", "Cf", "Zl", "Zp"} or _is_noncharacter(codepoint):
            raise ValidationError(
                f"{field} contains a prohibited control or formatting character "
                f"U+{codepoint:04X}"
            )

    flags = tuple(name for name, pattern in _SEMANTIC_FLAGS if pattern.search(value))
    return SanitizedText(value, flags)


def merge_risk_flags(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({flag for group in groups for flag in group}))


def terminal_lines(value: str, prefix: str = "  ") -> list[str]:
    """Render untrusted text without allowing terminal control sequences."""
    safe: list[str] = []
    for char in value:
        codepoint = ord(char)
        if char == "\n":
            safe.append(char)
        elif char == "\t":
            safe.append("    ")
        elif unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} or _is_noncharacter(
            codepoint
        ):
            safe.append(f"\\u{codepoint:04x}")
        else:
            safe.append(char)
    return [prefix + line for line in "".join(safe).split("\n")]
