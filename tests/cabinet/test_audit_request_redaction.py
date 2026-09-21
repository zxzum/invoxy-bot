from app.cabinet.dependencies import _redact_audit_value


def test_audit_request_redaction_covers_nested_credentials_and_secret_settings() -> None:
    redacted = _redact_audit_value(
        {
            'password': 'do-not-store',
            'nested': {'access_token': 'also-do-not-store'},
            'settings': {'key': 'CABINET_JWT_SECRET', 'value': 'jwt-secret'},
            'label': 'safe-to-store',
        }
    )

    assert redacted == {
        'password': '[REDACTED]',
        'nested': {'access_token': '[REDACTED]'},
        'settings': {'key': 'CABINET_JWT_SECRET', 'value': '[REDACTED]'},
        'label': 'safe-to-store',
    }
