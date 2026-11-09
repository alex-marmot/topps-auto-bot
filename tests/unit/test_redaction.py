from __future__ import annotations

from tests.support import (
    ADDRESS_MARKER,
    CARD_MARKER,
    EMAIL_MARKER,
    NAME_MARKER,
    SYNTHETIC_CVV,
    make_card,
    make_customer,
)
from topps_auto_bot.redaction import (
    Redactor,
    build_diagnostic_report,
    mask_card_number,
    mask_cvv,
    redact_text,
)


def test_card_and_cvv_masking() -> None:
    assert mask_card_number(CARD_MARKER) == "************1111"
    assert mask_card_number("1234") == "****"
    assert mask_cvv(SYNTHETIC_CVV) == "***"
    assert mask_cvv("") == "[未设置]"


def test_redactor_removes_known_credentials() -> None:
    redactor = Redactor.from_credentials(make_customer(), make_card())
    text = f"card={CARD_MARKER} cvv={SYNTHETIC_CVV} email={EMAIL_MARKER} address={ADDRESS_MARKER} name={NAME_MARKER}"
    redacted = redactor.redact_text(text)
    for secret in (CARD_MARKER, SYNTHETIC_CVV, EMAIL_MARKER, ADDRESS_MARKER, NAME_MARKER):
        assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_masks_luhn_card_and_key_values() -> None:
    text = "raw 4111111111111111 cvv=123 token: abc123"
    redacted = redact_text(text)
    assert "4111" not in redacted
    assert "cvv=" in redacted
    assert "abc123" not in redacted
    assert "123" not in redacted


def test_redact_value_masks_sensitive_keys_recursively() -> None:
    redactor = Redactor.from_credentials(make_customer(), make_card())
    payload = {
        "card_number": CARD_MARKER,
        "nested": {"cvv": SYNTHETIC_CVV, "order_id": "ORDER-123"},
        "message": f"failed for {ADDRESS_MARKER}",
    }
    redacted = redactor.redact_value(payload)
    assert redacted["card_number"] == "[REDACTED]"
    assert redacted["nested"]["cvv"] == "[REDACTED]"
    assert redacted["nested"]["order_id"] == "ORDER-123"
    assert ADDRESS_MARKER not in redacted["message"]


def test_exception_text_is_redacted() -> None:
    redactor = Redactor.from_credentials(make_customer(), make_card())
    message = redactor.redact_text(f"WebDriver error with card {CARD_MARKER} at {ADDRESS_MARKER}")
    assert CARD_MARKER not in message
    assert ADDRESS_MARKER not in message


def test_diagnostic_report_excludes_synthetic_secrets() -> None:
    redactor = Redactor.from_credentials(make_customer(), make_card())
    report = build_diagnostic_report(
        redactor=redactor,
        phase="SUBMITTING",
        events=[
            {
                "message": f"failed {CARD_MARKER} {SYNTHETIC_CVV} {ADDRESS_MARKER}",
                "details": {"email": EMAIL_MARKER, "session_token": "synthetic-token"},
            }
        ],
        submission_state="UNKNOWN",
        runtime={"card_number": CARD_MARKER, "address": ADDRESS_MARKER},
    )
    text = str(report)
    for marker in (CARD_MARKER, SYNTHETIC_CVV, ADDRESS_MARKER, EMAIL_MARKER, NAME_MARKER, "synthetic-token"):
        assert marker not in text
    assert report["screenshots"] == "disabled"
