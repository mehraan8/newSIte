"""PII guard for event payloads — enforcement in code, not convention.

Every event payload that reaches the outbox is run through `assert_clean_payload`
(wired into serialization.event_payload). Personal data must travel as `pii_refs`
opaque tokens, never as raw values, so the event log stays GDPR crypto-shreddable.

Two layers of defense:
  1. forbidden KEYS  — structural (a field literally named mobile/email/...)
  2. value HEURISTICS — catches PII smuggled under an innocent key (email/IR phone)
"""

from __future__ import annotations

import re
from collections.abc import Mapping


class PiiLeakageError(ValueError):
    """Raised when an event payload (or pii_ref) contains raw personal data."""


# Exact key names (case-insensitive) that must never appear in a payload.
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "mobile", "mobile_number", "phone", "phone_number", "msisdn", "tel",
        "email", "e_mail", "mail",
        "name", "full_name", "fullname", "first_name", "last_name",
        "given_name", "family_name",
        "national_id", "national_code", "nationalcode", "ssn",
        "passport", "passport_no",
        "address", "postal_code", "zip", "zipcode",
        "dob", "birth_date", "birthdate",
        "card_number", "pan", "iban", "cvv", "cvv2", "account_number",
    }
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Iranian mobile, canonical + common local forms — tight, low false-positive.
_PHONE_RES = (
    re.compile(r"^\+989\d{9}$"),
    re.compile(r"^09\d{9}$"),
    re.compile(r"^00989\d{9}$"),
)


def _looks_like_pii_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.replace(" ", "").replace("-", "")
    if _EMAIL_RE.match(value):
        return "email-like"
    if any(rx.match(v) for rx in _PHONE_RES):
        return "phone-like"
    return None


def assert_clean_payload(payload: object, *, path: str = "payload") -> object:
    """Recursively reject forbidden keys and PII-looking values. Returns payload."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise PiiLeakageError(f"forbidden PII key '{key}' at {path}")
            kind = _looks_like_pii_value(value)
            if kind:
                raise PiiLeakageError(f"{kind} value at {path}.{key}")
            assert_clean_payload(value, path=f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for i, value in enumerate(payload):
            kind = _looks_like_pii_value(value)
            if kind:
                raise PiiLeakageError(f"{kind} value at {path}[{i}]")
            assert_clean_payload(value, path=f"{path}[{i}]")
    return payload


def assert_clean_pii_refs(refs: object) -> None:
    """pii_refs must be opaque reference tokens, never raw values."""
    if refs is None:
        return
    if not isinstance(refs, (list, tuple)):
        raise PiiLeakageError("pii_refs must be a list/tuple of token strings")
    for ref in refs:
        if not isinstance(ref, str) or not ref:
            raise PiiLeakageError("each pii_ref must be a non-empty string token")
        if _looks_like_pii_value(ref):
            raise PiiLeakageError("pii_ref looks like raw PII; expected a reference token")
        if "://" not in ref and not ref.startswith("vault:"):
            raise PiiLeakageError(
                f"pii_ref '{ref}' is not an opaque reference (e.g. 'vault://...')"
            )
