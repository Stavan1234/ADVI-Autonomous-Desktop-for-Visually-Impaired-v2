from __future__ import annotations

from pathlib import Path
import os

from ...core.config import PROJECT_ROOT


SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
]

CREDENTIALS_FILE = Path(
    os.getenv("GMAIL_CREDENTIALS_PATH")
    or (PROJECT_ROOT / "credentials" / "gmail_credentials.json")
)

TOKEN_FILE = Path(
    os.getenv("GMAIL_TOKEN_PATH")
    or (CREDENTIALS_FILE.parent / "gmail_token.json")
)


def get_gmail_credentials():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError(
            "Google authentication packages are not installed."
        ) from exc

    credentials = None

    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(
            str(TOKEN_FILE),
            SCOPES,
        )

    if (
        credentials is not None
        and credentials.valid
    ):
        return credentials

    if (
        credentials is not None
        and credentials.expired
        and credentials.refresh_token
    ):
        try:
            credentials.refresh(Request())
        except Exception as exc:
            raise RuntimeError(
                "Gmail OAuth refresh failed. Delete credentials/gmail_token.json and authorize Gmail again."
            ) from exc

        if not credentials.valid:
            raise RuntimeError(
                "Gmail OAuth credentials remain invalid after refresh."
            )

    else:
        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                "Gmail OAuth credentials were not found at "
                f"{CREDENTIALS_FILE}"
            )

        flow = (
            InstalledAppFlow
            .from_client_secrets_file(
                str(CREDENTIALS_FILE),
                SCOPES,
            )
        )

        credentials = flow.run_local_server(
            port=0,
            open_browser=True,
        )

    TOKEN_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    TOKEN_FILE.write_text(
        credentials.to_json(),
        encoding="utf-8",
    )

    return credentials