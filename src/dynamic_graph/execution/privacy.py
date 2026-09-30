"""Restricted-data checks, independent of recording and custom log formatting."""

import re

from pydantic import JsonValue

REDACTED = "[REDACTED]"
SENSITIVE_KEYS = frozenset({"api_key", "password", "access_token", "authorization"})
API_KEY = re.compile(r"\b(?:sk|tvly)-[A-Za-z0-9_-]{16,}")


def _redact_string(value: str, sensitive_values: tuple[str, ...]) -> str:
    for secret in sensitive_values:
        if secret:
            value = value.replace(secret, REDACTED)
    return API_KEY.sub(REDACTED, value)


def contains_sensitive(value: JsonValue, sensitive_values: tuple[str, ...] = ()) -> bool:
    """Check built-in credential patterns and explicitly configured restricted values."""
    if isinstance(value, str):
        return _redact_string(value, sensitive_values) != value
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in SENSITIVE_KEYS:
                if item != REDACTED:
                    return True
            elif contains_sensitive(item, sensitive_values):
                return True
        return False
    if isinstance(value, list):
        return any(contains_sensitive(item, sensitive_values) for item in value)
    return False


def redact_sensitive(value: JsonValue, sensitive_values: tuple[str, ...] = ()) -> JsonValue:
    """Return an isolated JSON value with restricted data replaced."""
    if isinstance(value, str):
        return _redact_string(value, sensitive_values)
    if isinstance(value, dict):
        return {
            key: REDACTED
            if key.lower() in SENSITIVE_KEYS
            else redact_sensitive(item, sensitive_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item, sensitive_values) for item in value]
    return value
