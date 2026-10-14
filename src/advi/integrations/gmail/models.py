from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GmailMessage:
    id: str
    thread_id: str

    sender: str = ""
    recipients: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    date: str = ""
    body: str = ""
    snippet: str = ""
    label_ids: tuple[str, ...] = field(default_factory=tuple)
    internal_date: str = ""
    message_id_header: str = ""
    references: str = ""

@dataclass(frozen=True)
class GmailDraft:
    draft_id: str
    recipient: str
    subject: str
    body: str

@dataclass(frozen=True)
class GmailThread:
    thread_id: str
    messages: tuple[GmailMessage, ...] = field(default_factory=tuple)
    history_id: str = ""

    @property
    def latest_message(self) -> GmailMessage | None:
        return self.messages[-1] if self.messages else None

    @property
    def subject(self) -> str:
        return next((m.subject for m in reversed(self.messages) if m.subject), "")
