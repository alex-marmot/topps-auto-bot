from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import Card, Customer, LoginCredentials

REDACTED = "[REDACTED]"

_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_KEY_VALUE = re.compile(
    r"(?i)\b(cvv2?|cvc2?|card[ _-]?number|card[ _-]?no|pan|password|passwd|token|"
    r"cookie|session[ _-]?id)\b(\s*[:=]\s*)([^\s,;]+)"
)
_SENSITIVE_KEY_PARTS = (
    "card",
    "cvv",
    "cvc",
    "password",
    "token",
    "cookie",
    "session",
    "secret",
    "address",
    "email",
    "phone",
)


def _luhn_like(value: str) -> bool:
    digits = [int(ch) for ch in value if ch.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def mask_card_number(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    if not digits:
        return "[未设置]"
    if len(digits) <= 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + digits[-4:]


def mask_cvv(cvv: str) -> str:
    return "*" * max(3, len(cvv)) if cvv else "[未设置]"


def mask_password(password: str) -> str:
    return "*" * 8 if password else "[未设置]"


@dataclass(frozen=True)
class Redactor:
    secrets: tuple[str, ...] = ()

    @classmethod
    def from_credentials(
        cls,
        customer: Customer,
        card: Card,
        login: LoginCredentials | None = None,
    ) -> "Redactor":
        values = [
            card.number,
            card.cvv,
            card.holder_name,
            customer.full_name,
            customer.email,
            customer.phone,
            customer.address_line1,
            customer.address_line2,
            customer.postal_code,
        ]
        if login is not None:
            values.extend([login.email, login.password])
        return cls(tuple(sorted({v for v in values if v and len(v) >= 3}, key=len, reverse=True)))

    def redact_text(self, text: str) -> str:
        if not text:
            return text
        result = text
        for secret in self.secrets:
            if not secret:
                continue
            if len(secret) >= 8:
                result = result.replace(secret, REDACTED)
            else:
                result = re.sub(
                    rf"(?<![\w-]){re.escape(secret)}(?![\w-])",
                    REDACTED,
                    result,
                )
        result = _EMAIL.sub("[REDACTED_EMAIL]", result)
        result = _KEY_VALUE.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", result)

        def _card_replacement(match: re.Match[str]) -> str:
            candidate = match.group(0)
            if _luhn_like(candidate):
                return mask_card_number(candidate)
            return candidate

        return _CARD_CANDIDATE.sub(_card_replacement, result)

    def redact_value(self, value: Any, *, key: str | None = None) -> Any:
        if key is not None and any(part in key.casefold() for part in _SENSITIVE_KEY_PARTS):
            if value in (None, "", False):
                return value
            return REDACTED
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {item_key: self.redact_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_value(item) for item in value]
        return value

    def redact_diagnostics(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.redact_value(payload)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    return Redactor(tuple(sorted({s for s in secrets if s and len(s) >= 3}, key=len, reverse=True))).redact_text(text)


def build_diagnostic_report(
    *,
    redactor: Redactor,
    phase: str,
    events: Iterable[dict[str, Any]],
    submission_state: str | None,
    runtime: dict[str, Any],
    created_at: datetime | None = None,
) -> dict[str, Any]:
    timestamp = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "created_at_utc": timestamp,
        "phase": phase,
        "submission_state": submission_state,
        "runtime": runtime,
        "events": list(events),
        "screenshots": "disabled",
        "note": "诊断仅含脱敏状态，不包含配置正文、浏览器会话、支付页面或交易凭据。",
    }
    return redactor.redact_diagnostics(payload)
