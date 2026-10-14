from __future__ import annotations

import importlib


def test_gmail_credentials_path_can_be_configured_via_environment(monkeypatch, tmp_path):
    credentials = tmp_path / "oauth" / "client.json"
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(credentials))
    monkeypatch.delenv("GMAIL_TOKEN_PATH", raising=False)

    import advi.integrations.gmail.auth as auth
    auth = importlib.reload(auth)

    assert auth.CREDENTIALS_FILE == credentials
    assert auth.TOKEN_FILE == credentials.parent / "gmail_token.json"


def test_gmail_service_uses_configured_credentials_paths(monkeypatch, tmp_path):
    credentials = tmp_path / "client.json"
    token = tmp_path / "token.json"
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(credentials))
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token))

    import advi.integrations.gmail.auth as auth
    auth = importlib.reload(auth)
    import advi.integrations.gmail.service as service
    service = importlib.reload(service)

    gmail = service.LazyGmailService()
    assert not gmail.is_available()
    credentials.write_text("{}", encoding="utf-8")
    assert gmail.is_available()
    token.write_text("{}", encoding="utf-8")
    assert gmail.is_available()
