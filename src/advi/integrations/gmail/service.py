from __future__ import annotations

import base64
from email.mime.text import MIMEText
from pathlib import Path
import re
from email.utils import parseaddr

from ...core.pipeline_trace import trace, trace_exception

from .auth import CREDENTIALS_FILE, TOKEN_FILE, get_gmail_credentials

from .models import (
    GmailDraft,
    GmailMessage,
    GmailThread,
)

class LazyGmailService:
    """Initialize the Gmail API only when an email operation is actually requested."""

    def __init__(self) -> None:
        self._service: GmailService | None = None
        self._startup_prepared = False
        self._lazy_initialization_frozen = False
        self._initialization_error: Exception | None = None

    @property
    def initialized(self) -> bool:
        return self._service is not None

    def is_available(self) -> bool:
        return self._service is not None or CREDENTIALS_FILE.exists() or TOKEN_FILE.exists()

    def prepare(self) -> bool:
        """Prepare Gmail at startup when a saved OAuth token already exists.

        A first-time interactive OAuth flow is intentionally left lazy so a
        missing token cannot block ADVI startup by opening a browser. Once a
        token exists, its API client and refresh path are prepared before chat.
        """
        if self._service is not None:
            self._startup_prepared = True
            return True
        if not TOKEN_FILE.exists():
            return False
        try:
            self._get()
        except Exception as exc:
            self._initialization_error = exc
            raise
        self._startup_prepared = True
        return True

    def freeze_lazy_initialization(self) -> None:
        """Prevent implicit attribute access from starting Gmail after startup.

        First-use authentication is still allowed through the explicit
        :meth:`ensure_ready` action boundary used by ``GmailCapability``.
        """
        self._lazy_initialization_frozen = True

    def ensure_ready(self) -> bool:
        """Explicitly initialize Gmail for a user-requested Gmail action.

        Startup intentionally does not launch first-use OAuth.  A real Gmail
        action is the explicit boundary at which interactive authentication may
        begin, including Google's account-selection flow.
        """
        if self._service is not None:
            return True

        self._initialization_error = None
        try:
            self._get()
        except Exception as exc:
            self._initialization_error = exc
            raise

        return self._service is not None

    @property
    def startup_prepared(self) -> bool:
        return self._startup_prepared

    def _get(self) -> "GmailService":
        if self._service is None:
            self._service = GmailService()
        return self._service

    def close(self) -> None:
        if self._service is not None:
            self._service.close()
            self._service = None

    def __getattr__(self, name):
        if self._lazy_initialization_frozen and self._service is None:
            if self._initialization_error is not None:
                raise RuntimeError(
                    "Gmail is not initialized for this session. "
                    f"Use an explicit Gmail action to authenticate: {self._initialization_error}"
                ) from self._initialization_error
            raise RuntimeError(
                "Gmail is not initialized for this session. "
                "A user-requested Gmail action must call ensure_ready() first."
            )
        return getattr(self._get(), name)


class GmailService:
    """Small wrapper around the Gmail API used by ADVI."""

    def close(self) -> None:
        """Close the underlying HTTP transport when the client exposes one."""
        service = getattr(self, "_service", None)
        http = getattr(service, "_http", None)
        closer = getattr(http, "close", None)
        if callable(closer):
            closer()

    def __init__(self) -> None:
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Google API client is not installed."
            ) from exc

        credentials = get_gmail_credentials()

        self._service = build(
            "gmail",
            "v1",
            credentials=credentials,
        )


    @staticmethod
    def _validate_email_address(value: str) -> str:
        text = (value or "").strip()
        if not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", text):
            raise ValueError(f"Invalid email address: {value!r}")
        return text

    def health_check(self) -> bool:
        """Verify that the authenticated Gmail API session is usable."""
        self._service.users().getProfile(userId="me").execute()
        return True

    def list_drafts(self, limit: int = 10) -> list[dict]:
        response = (
            self._service.users()
            .drafts()
            .list(userId="me", maxResults=max(1, int(limit)))
            .execute()
        )
        return response.get("drafts", [])

    def list_messages(
        self,
        query: str = "",
        max_results: int = 10,
        *,
        label_ids: list[str] | None = None,
        include_spam_trash: bool = False,
        page_token: str | None = None,
    ) -> list[dict]:
        """List Gmail message stubs, newest first, with bounded pagination.

        Gmail's messages.list returns IDs/thread IDs only; callers that need
        content should use get_message/get_parsed_message.
        """
        if max_results < 1:
            return []
        kwargs = {
            "userId": "me",
            "maxResults": min(int(max_results), 500),
            "includeSpamTrash": bool(include_spam_trash),
        }
        if query:
            kwargs["q"] = query.strip()
        if label_ids:
            kwargs["labelIds"] = list(label_ids)
        if page_token:
            kwargs["pageToken"] = page_token
        response = self._service.users().messages().list(**kwargs).execute()
        return response.get("messages", [])

    def list_message_pages(
        self,
        query: str = "",
        max_results: int = 10,
        *,
        label_ids: list[str] | None = None,
        include_spam_trash: bool = False,
        max_pages: int = 1,
    ) -> list[dict]:
        """Retrieve a bounded number of pages without unbounded mailbox scans."""
        rows: list[dict] = []
        token: str | None = None
        for _ in range(max(1, int(max_pages))):
            kwargs = {
                "userId": "me",
                "maxResults": min(max(1, int(max_results)), 500),
                "includeSpamTrash": bool(include_spam_trash),
            }
            if query:
                kwargs["q"] = query.strip()
            if label_ids:
                kwargs["labelIds"] = list(label_ids)
            if token:
                kwargs["pageToken"] = token
            response = self._service.users().messages().list(**kwargs).execute()
            rows.extend(response.get("messages", []))
            token = response.get("nextPageToken")
            if not token:
                break
        return rows

    def search_messages(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict]:
        return self.list_messages(query=query, max_results=limit)


    def search_parsed_messages(
        self,
        query: str,
        limit: int = 10,
    ) -> list[GmailMessage]:
        rows = self.search_messages(
            query,
            limit=limit,
        )

        messages: list[GmailMessage] = []

        for row in rows:
            message_id = row.get("id")

            if not message_id:
                continue

            messages.append(
                self.get_parsed_message(
                    message_id
                )
            )

        return messages    

    def list_threads(
        self,
        query: str = "",
        max_results: int = 10,
        *,
        label_ids: list[str] | None = None,
        include_spam_trash: bool = False,
    ) -> list[dict]:
        """List conversation/thread stubs, newest first."""
        kwargs = {
            "userId": "me",
            "maxResults": min(max(1, int(max_results)), 100),
            "includeSpamTrash": bool(include_spam_trash),
        }
        if query:
            kwargs["q"] = query.strip()
        if label_ids:
            kwargs["labelIds"] = list(label_ids)
        response = self._service.users().threads().list(**kwargs).execute()
        return response.get("threads", [])

    def get_thread(self, thread_id: str, *, format: str = "full") -> GmailThread:
        if not thread_id.strip():
            raise ValueError("Thread ID cannot be empty.")
        response = (
            self._service.users().threads().get(
                userId="me", id=thread_id, format=format
            ).execute()
        )
        messages = tuple(
            self._normalize_message(raw) for raw in response.get("messages", [])
        )
        return GmailThread(
            thread_id=response.get("id", thread_id),
            messages=messages,
            history_id=response.get("historyId", ""),
        )

    def _normalize_message(self, raw: dict) -> GmailMessage:
        payload = raw.get("payload", {})
        headers = {
            str(header.get("name", "")).lower(): str(header.get("value", ""))
            for header in payload.get("headers", [])
        }
        return GmailMessage(
            id=raw.get("id", ""),
            thread_id=raw.get("threadId", ""),
            sender=headers.get("from", ""),
            recipients=headers.get("to", ""),
            cc=headers.get("cc", ""),
            bcc=headers.get("bcc", ""),
            subject=headers.get("subject", ""),
            date=headers.get("date", ""),
            body=self._extract_body(payload),
            snippet=raw.get("snippet", ""),
            label_ids=tuple(raw.get("labelIds", []) or []),
            internal_date=str(raw.get("internalDate", "")),
            message_id_header=headers.get("message-id", ""),
            references=headers.get("references", ""),
        )

    def get_message(
        self,
        message_id: str,
    ) -> dict:
        return (
            self._service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="full",
            )
            .execute()
        )

    def get_parsed_message(
        self,
        message_id: str,
    ) -> GmailMessage:
        return self._normalize_message(self.get_message(message_id))

    @staticmethod
    def _extract_body(
        payload: dict,
    ) -> str:
        mime_type = payload.get(
            "mimeType",
            "",
        )

        body_data = (
            payload.get("body", {})
            .get("data")
        )

        # Prefer plain text.
        if (
            body_data
            and mime_type.startswith(
                "text/plain"
            )
        ):
            return GmailService._decode_body(
                body_data
            )

        # Search nested parts for plain text
        # before considering HTML.
        for part in payload.get(
            "parts",
            [],
        ):
            if (
                part.get("mimeType", "")
                .startswith("text/plain")
            ):
                part_data = (
                    part.get("body", {})
                    .get("data")
                )

                if part_data:
                    return (
                        GmailService._decode_body(
                            part_data
                        )
                    )

        # Recursively search deeper multipart
        # structures for plain text.
        for part in payload.get(
            "parts",
            [],
        ):
            result = GmailService._extract_body(
                part
            )

            if result:
                return result

        # Only fall back to HTML if no plain text
        # was found anywhere.
        if (
            body_data
            and mime_type.startswith(
                "text/html"
            )
        ):
            return GmailService._decode_body(
                body_data
            )

        return ""

    @staticmethod
    def _decode_body(
        data: str,
    ) -> str:
        import base64

        decoded = base64.urlsafe_b64decode(
            data + "=" * (
                -len(data) % 4
            )
        )

        return decoded.decode(
            "utf-8",
            errors="replace",
        )    

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
    ) -> GmailDraft:
        to = self._validate_email_address(to)
        subject = (subject or "").strip()
        if not body.strip():
            raise ValueError("Email body cannot be empty.")
        trace(
            "gmail.create_draft.request",
            source_component="GmailService",
            to=to,
            subject=subject,
            body_length=len(body),
        )

        message = MIMEText(
            body,
            _subtype="plain",
            _charset="utf-8",
        )

        message["To"] = to
        message["Subject"] = subject

        encoded = base64.urlsafe_b64encode(
            message.as_bytes()
        ).decode(
            "ascii"
        )

        try:
            response = (
                self._service.users()
                .drafts()
                .create(
                    userId="me",
                    body={
                        "message": {
                            "raw": encoded,
                        }
                    },
                )
                .execute()
            )
        except Exception as exc:
            trace_exception(
                "gmail.create_draft.error",
                exc,
                to=to,
                subject=subject,
            )
            raise

        draft = GmailDraft(
            draft_id=response.get(
                "id",
                "",
            ),
            recipient=to,
            subject=subject,
            body=body,
        )

        trace(
            "gmail.create_draft.response",
            success=True,
            draft_id=draft.draft_id,
            recipient=draft.recipient,
            subject=draft.subject,
            message_id=response.get("message", {}).get("id"),
            thread_id=response.get("message", {}).get("threadId"),
        )

        return draft


    def get_draft(
        self,
        draft_id: str,
    ) -> GmailDraft:
        trace(
            "gmail.get_draft.request",
            source_component="GmailService",
            draft_id=draft_id,
        )

        try:
            response = (
                self._service.users()
                .drafts()
                .get(
                    userId="me",
                    id=draft_id,
                    format="full",
                )
                .execute()
            )
        except Exception as exc:
            trace_exception(
                "gmail.get_draft.error",
                exc,
                draft_id=draft_id,
            )
            raise

        message = response.get(
            "message",
            {},
        )

        payload = message.get(
            "payload",
            {},
        )

        headers = {
            header.get("name", "").lower():
            header.get("value", "")
            for header in payload.get(
                "headers",
                [],
            )
        }

        draft = GmailDraft(
            draft_id=response.get(
                "id",
                draft_id,
            ),
            recipient=headers.get(
                "to",
                "",
            ),
            subject=headers.get(
                "subject",
                "",
            ),
            body=self._extract_body(
                payload
            ),
        )

        trace(
            "gmail.get_draft.response",
            success=True,
            draft_id=draft.draft_id,
            recipient=draft.recipient,
            subject=draft.subject,
            body_length=len(draft.body),
        )

        return draft  

    def update_draft(
        self,
        draft_id: str,
        to: str,
        subject: str,
        body: str,
    ) -> GmailDraft:
        if not draft_id.strip():
            raise ValueError("Draft ID cannot be empty.")
        to = self._validate_email_address(to)
        subject = (subject or "").strip()
        if not body.strip():
            raise ValueError("Email body cannot be empty.")
        trace(
            "gmail.update_draft.request",
            source_component="GmailService",
            draft_id=draft_id,
            to=to,
            subject=subject,
            body_length=len(body),
        )

        message = MIMEText(
            body,
            _subtype="plain",
            _charset="utf-8",
        )

        message["To"] = to
        message["Subject"] = subject

        encoded = base64.urlsafe_b64encode(
            message.as_bytes()
        ).decode("ascii")

        try:
            response = (
                self._service.users()
                .drafts()
                .update(
                    userId="me",
                    id=draft_id,
                    body={
                        "message": {
                            "raw": encoded,
                        }
                    },
                )
                .execute()
            )
        except Exception as exc:
            trace_exception(
                "gmail.update_draft.error",
                exc,
                draft_id=draft_id,
            )
            raise

        draft = GmailDraft(
            draft_id=response.get(
                "id",
                draft_id,
            ),
            recipient=to,
            subject=subject,
            body=body,
        )

        trace(
            "gmail.update_draft.response",
            success=True,
            draft_id=draft.draft_id,
            recipient=draft.recipient,
            subject=draft.subject,
        )

        return draft    

    def send_draft(
        self,
        draft_id: str,
    ) -> dict:
        if not draft_id.strip():
            raise ValueError("Draft ID cannot be empty.")
        trace(
            "gmail.send_draft.request",
            source_component="GmailService",
            draft_id=draft_id,
        )

        try:
            response = (
                self._service.users()
                .drafts()
                .send(
                    userId="me",
                    body={
                        "id": draft_id,
                    },
                )
                .execute()
            )
        except Exception as exc:
            trace_exception(
                "gmail.send_draft.error",
                exc,
                draft_id=draft_id,
            )
            raise

        trace(
            "gmail.send_draft.response",
            success=True,
            draft_id=draft_id,
            message_id=response.get("id"),
            thread_id=response.get("threadId"),
            label_ids=response.get("labelIds"),
        )

        return response

    def reply_to_message(self, message_id: str, body: str) -> dict:
        """Send a threaded reply to one concrete Gmail message."""
        message_id = (message_id or "").strip()
        body = (body or "").strip()
        if not message_id:
            raise ValueError("Message ID cannot be empty.")
        if not body:
            raise ValueError("Reply body cannot be empty.")

        original = self.get_message(message_id)
        payload = original.get("payload", {}) if isinstance(original, dict) else {}
        headers = {
            str(header.get("name", "")).lower(): str(header.get("value", ""))
            for header in payload.get("headers", [])
        }
        recipient_raw = (headers.get("reply-to") or headers.get("from") or "").strip()
        _display_name, recipient = parseaddr(recipient_raw)
        recipient = self._validate_email_address(recipient or recipient_raw)
        original_subject = (headers.get("subject") or "").strip()
        subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}" if original_subject else "Re:"

        message = MIMEText(body, _subtype="plain", _charset="utf-8")
        message["To"] = recipient
        message["Subject"] = subject
        message_header = headers.get("message-id", "").strip()
        references = headers.get("references", "").strip()
        if message_header:
            message["In-Reply-To"] = message_header
            message["References"] = f"{references} {message_header}".strip()
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        thread_id = str(original.get("threadId") or "").strip()
        body_payload: dict[str, str] = {"raw": raw}
        if thread_id:
            body_payload["threadId"] = thread_id

        trace("gmail.reply.request", source_component="GmailService", in_reply_to=message_id, recipient=recipient, subject=subject, body_length=len(body))
        try:
            response = self._service.users().messages().send(userId="me", body=body_payload).execute()
        except Exception as exc:
            trace_exception("gmail.reply.error", exc, in_reply_to=message_id, recipient=recipient, subject=subject)
            raise
        trace("gmail.reply.response", success=True, in_reply_to=message_id, message_id=response.get("id"), thread_id=response.get("threadId"))
        return response

    def send_email(self, to: str, subject: str, body: str) -> dict:
        """Create and immediately send one message via Gmail messages.send."""
        to = self._validate_email_address(to)
        subject = (subject or "").strip()
        if not body.strip():
            raise ValueError("Email body cannot be empty.")

        message = MIMEText(body, _subtype="plain", _charset="utf-8")
        message["To"] = to
        message["Subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

        trace("gmail.send_email.request", source_component="GmailService", to=to, subject=subject, body_length=len(body))
        try:
            response = (
                self._service.users()
                .messages()
                .send(userId="me", body={"raw": raw})
                .execute()
            )
        except Exception as exc:
            trace_exception("gmail.send_email.error", exc, to=to, subject=subject)
            raise

        trace("gmail.send_email.response", success=True, message_id=response.get("id"), thread_id=response.get("threadId"))
        return response
