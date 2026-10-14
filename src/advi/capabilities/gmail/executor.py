from __future__ import annotations

import html
import re
import logging
from typing import Any

from advi.tools.contracts import ToolCall, ToolResult
from advi.integrations.gmail import GmailService

logger = logging.getLogger(__name__)




def _compact_email_body(value: Any, *, limit: int = 2200) -> str:
    """Convert rich Gmail bodies to bounded speech/LLM-friendly plain text."""
    text = str(value or "")
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>|</div>|</li>|</tr>|</h[1-6]>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > limit:
        marker = " … [body truncated]"
        cut = max(0, limit - len(marker))
        text = text[:cut].rstrip() + marker
    return text


class GmailCapability:
    """
    Gmail Integration Capability.
    Provides email drafting, reading, updating, and sending via GmailService.
    """

    SUPPORTED_ACTIONS = {
        "email_draft_create",
        "email_draft_read",
        "email_draft_update",
        "email_send",
        "email_reply",
        "email_read",
    }

    def __init__(self, service: GmailService | None = None) -> None:
        self.service = service

    def is_available(self) -> bool:
        return self.service is not None

    def execute(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        if not self.service:
            return self._failure(
                action,
                "Gmail service is not configured or authenticated.",
                fallback_recommended=True,
                failure_class="gmail_unavailable",
            )

        handler = getattr(self, f"_execute_{action.action}", None)
        if handler is None:
            return self._failure(action, f"Unsupported Gmail action: {action.action}", failure_class="invalid_input")

        try:
            ensure_ready = getattr(self.service, "ensure_ready", None)
            if callable(ensure_ready):
                ensure_ready()
            return handler(action)
        except ValueError as exc:
            logger.warning("Gmail action rejected locally: %s", exc)
            return self._failure(action, str(exc), failure_class="invalid_input")
        except Exception as exc:
            logger.exception("Gmail action failed: %s", action.action)
            safe_fallback = self._safe_pre_execution_failure(exc)
            return self._failure(
                action,
                str(exc),
                fallback_recommended=safe_fallback,
                failure_class=("gmail_unavailable" if safe_fallback else "uncertain_external_effect"),
            )

    @staticmethod
    def _safe_pre_execution_failure(exc: Exception) -> bool:
        """Only classify known pre-execution HTTP errors as safe to retry via UI."""
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status is None:
            status = getattr(exc, "status", None)
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        return status in {400, 401, 403, 404, 405, 409, 412, 422}

    @staticmethod
    def _fallback_context(action: ToolCall) -> dict[str, Any]:
        args = dict(action.arguments)
        context: dict[str, Any] = {"operation": action.name}
        for key in ("to", "recipient", "subject", "body", "content", "draft_id", "message_id"):
            value = args.get(key)
            if value not in (None, ""):
                context[key] = value
        return context

    @classmethod
    def _failure(
        cls,
        action: ToolCall,
        error: str,
        *,
        failure_class: str,
        fallback_recommended: bool = False,
    ) -> ToolResult:
        metadata: dict[str, Any] = {"failure_class": failure_class}
        output: Any = None
        if fallback_recommended:
            context = cls._fallback_context(action)
            output = {
                "fallback": {
                    "recommended": True,
                    "system": "david",
                    "task": context,
                    "instruction": (
                        "Use David through the browser/desktop path. Preserve the exact Gmail "
                        "recipient, subject, body/content, and draft_id supplied here. Do not ask "
                        "the user to provide browser or Gmail steps."
                    ),
                }
            }
            metadata["fallback_recommended"] = True
            metadata["fallback_context"] = context
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=False,
            output=output,
            error=error,
            verified=False,
            metadata=metadata,
        )

    def _execute_email_draft_create(self, action: ToolCall) -> ToolResult:
        to = action.parameters.get("to") or action.parameters.get("recipient", "")
        subject = action.parameters.get("subject", "")
        body = action.parameters.get("body") or action.parameters.get("content", "")

        draft = self.service.create_draft(to=to, subject=subject, body=body)
        # GmailDraft is a dataclass with .draft_id, .recipient, .subject, .body
        draft_id = draft.draft_id if hasattr(draft, "draft_id") else (draft.get("id", "") if isinstance(draft, dict) else "")
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output={"draft_id": draft_id, "to": to, "subject": subject},
            human_readable=f"Created email draft to '{to}' with subject '{subject}'.",
            verified=bool(draft_id),
            verification_details={"draft_id": draft_id},
            metadata={"draft_id": draft_id, "to": to, "subject": subject},
        )

    def _execute_email_draft_read(self, action: ToolCall) -> ToolResult:
        draft_id = action.parameters.get("draft_id") or action.target or ""
        draft = self.service.get_draft(draft_id) if hasattr(self.service, "get_draft") else None
        if draft:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=True,
                output=draft,
                human_readable=f"Retrieved email draft {draft_id}.",
                verified=True,
                verification_status="verified",
                verification_details={"method": "gmail_draft_fetch", "draft_id": draft_id},
                metadata={"draft_id": draft_id, "to": getattr(draft, "recipient", ""), "subject": getattr(draft, "subject", ""), "body": getattr(draft, "body", "")},
            )
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=False,
            error=f"Could not retrieve draft {draft_id}.",
        )

    def _execute_email_draft_update(self, action: ToolCall) -> ToolResult:
        draft_id = action.parameters.get("draft_id") or action.target or ""
        to = action.parameters.get("to") or action.parameters.get("recipient", "")
        subject = action.parameters.get("subject", "")
        body = action.parameters.get("body") or action.parameters.get("content", "")

        if hasattr(self.service, "update_draft"):
            res = self.service.update_draft(draft_id=draft_id, to=to, subject=subject, body=body)
            updated_id = (
                res.draft_id if hasattr(res, "draft_id")
                else (res.get("id", draft_id) if isinstance(res, dict) else draft_id)
            )
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=True,
                output=res,
                human_readable=f"Updated draft {draft_id}.",
                verified=bool(updated_id),
                verification_details={"draft_id": updated_id},
                metadata={"draft_id": updated_id, "to": to, "subject": subject},
            )
        # Fallback: create updated draft
        return self._execute_email_draft_create(action)

    def _execute_email_send(self, action: ToolCall) -> ToolResult:
        to = action.parameters.get("to") or action.parameters.get("recipient", "")
        subject = action.parameters.get("subject", "")
        body = action.parameters.get("body") or action.parameters.get("content", "")
        draft_id = str(action.parameters.get("draft_id") or "").strip()

        if draft_id:
            # Recover the draft metadata when the model intentionally sends by draft_id
            # only. This keeps confirmation/final narration grounded in the actual draft.
            if not to or not subject:
                get_draft = getattr(self.service, "get_draft", None)
                if callable(get_draft):
                    try:
                        draft = get_draft(draft_id)
                        if isinstance(draft, dict):
                            to = str(draft.get("recipient") or draft.get("to") or to)
                            subject = str(draft.get("subject") or subject)
                            body = str(draft.get("body") or body)
                        else:
                            to = str(getattr(draft, "recipient", "") or getattr(draft, "to", "") or to)
                            subject = str(getattr(draft, "subject", "") or subject)
                            body = str(getattr(draft, "body", "") or body)
                    except Exception:
                        pass
            res = self.service.send_draft(draft_id)
        else:
            if not to or not body:
                return ToolResult(
                    call_id=action.call_id,
                    name=action.action,
                    success=False,
                    error="Sending an email requires a recipient and body, or an existing draft_id.",
                    metadata={"failure_class": "invalid_input"},
                )
            res = self.service.send_email(to=to, subject=subject, body=body)

        msg_id = res.get("id", "") if isinstance(res, dict) else str(res)
        recipient_display = to or "the saved Gmail draft"
        verified = bool(msg_id)
        verification_detail: dict[str, Any] = {"message_id": msg_id}
        # A returned Gmail message id is strong send evidence. Confirm that the
        # sent message can actually be fetched before calling the operation fully
        # verified; this protects against adapters that return an id without a
        # readable post-send resource.
        if msg_id:
            try:
                fetched = self.service.get_message(msg_id)
                verified = bool(fetched.get("id") == msg_id) if isinstance(fetched, dict) else verified
                verification_detail["message_fetch_verified"] = verified
            except Exception as exc:
                verification_detail["message_fetch_error"] = str(exc)[:500]

            # Gmail's stored sent message is the final source of truth after a
            # draft was edited. Prefer its parsed headers over stale model/tool
            # arguments so the user hears the actual sent subject and recipient.
            get_parsed = getattr(self.service, "get_parsed_message", None)
            if callable(get_parsed):
                try:
                    sent = get_parsed(msg_id)
                    actual_to = str(getattr(sent, "recipients", "") or "").strip()
                    actual_subject = str(getattr(sent, "subject", "") or "").strip()
                    if actual_to:
                        to = actual_to
                    if actual_subject:
                        subject = actual_subject
                    verification_detail["sent_message_headers_verified"] = True
                except Exception as exc:
                    verification_detail["sent_message_headers_error"] = str(exc)[:500]

        recipient_display = to or recipient_display
        sent_human = (
            f"Sent email to '{recipient_display}' with subject '{subject}'."
            if recipient_display and recipient_display != "the saved Gmail draft"
            else f"Sent the Gmail draft '{draft_id}'."
        )
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=res,
            human_readable=sent_human,
            verified=verified,
            verification_status="verified" if verified else "uncertain",
            verification_details=verification_detail,
            metadata={"message_id": msg_id, "recipient": to, "subject": subject, "draft_id": draft_id},
        )

    def _execute_email_reply(self, action: ToolCall) -> ToolResult:
        message_id = str(action.parameters.get("message_id") or action.parameters.get("id") or "").strip()
        body = str(action.parameters.get("body") or action.parameters.get("content") or action.parameters.get("text") or "").strip()
        if not message_id:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="A message_id is required to reply to an email.")
        if not body:
            return ToolResult(call_id=action.call_id, name=action.action, success=False, error="A reply body is required.")

        reply = self.service.reply_to_message(message_id=message_id, body=body)
        sent_id = reply.get("id", "") if isinstance(reply, dict) else str(reply)
        verified = False
        verification_detail: dict[str, Any] = {"message_id": sent_id, "in_reply_to": message_id}
        if sent_id:
            try:
                fetched = self.service.get_message(sent_id)
                verified = bool(isinstance(fetched, dict) and fetched.get("id") == sent_id)
                verification_detail["message_fetch_verified"] = verified
            except Exception as exc:
                verification_detail["message_fetch_error"] = str(exc)[:500]
        return ToolResult(
            call_id=action.call_id, name=action.action, success=True, output=reply,
            human_readable="Replied to the email.", verified=verified,
            verification_status="verified" if verified else "uncertain",
            verification_details=verification_detail,
            metadata={"message_id": sent_id, "in_reply_to": message_id},
        )

    def _execute_email_read(self, action: ToolCall) -> ToolResult:
        query = str(action.parameters.get("query", "is:inbox") or "is:inbox").strip()
        try:
            max_results = max(1, min(int(action.parameters.get("limit", 5) or 5), 20))
        except (TypeError, ValueError):
            max_results = 5

        # Prefer the rich search/parse API. Keep a compatibility path for
        # lightweight adapters and fakes that only expose list_messages; an
        # empty inbox is still a successful, verified read.
        search_messages = getattr(self.service, "search_messages", None)
        if callable(search_messages):
            rows = search_messages(query=query, limit=max_results)
        else:
            list_messages = getattr(self.service, "list_messages", None)
            if not callable(list_messages):
                raise AttributeError("Gmail service exposes neither search_messages nor list_messages")
            try:
                rows = list_messages(query=query, max_results=max_results)
            except TypeError:
                rows = list_messages(query, max_results)

        parsed: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        get_parsed_message = getattr(self.service, "get_parsed_message", None)
        for row in rows[:max_results]:
            if hasattr(row, "message_id"):
                message_id = str(getattr(row, "message_id") or "").strip()
            else:
                message_id = str(row.get("id") or "").strip() if isinstance(row, dict) else ""
            if not message_id:
                # Some test/light adapters may already return parsed rows.
                if isinstance(row, dict):
                    parsed.append(dict(row))
                continue
            if not callable(get_parsed_message):
                if isinstance(row, dict):
                    parsed.append(dict(row))
                continue
            try:
                message = get_parsed_message(message_id)
                include_full_body = max_results == 1
                item = {
                    "id": message.id,
                    "thread_id": message.thread_id,
                    "from": message.sender,
                    "to": message.recipients,
                    "cc": message.cc,
                    "subject": message.subject,
                    "date": message.date,
                    "snippet": _compact_email_body(message.snippet, limit=600),
                    "labels": list(message.label_ids),
                }
                if include_full_body:
                    item["body"] = _compact_email_body(message.body, limit=4200)
                parsed.append(item)
            except Exception as exc:
                failures.append({"message_id": message_id, "error": str(exc)[:500]})

        if not parsed and rows and failures:
            return ToolResult(
                call_id=action.call_id, name=action.action, success=False, output=failures,
                error=f"Gmail found {len(rows)} matching message(s), but their contents could not be read.",
                metadata={"count": 0, "matched_count": len(rows), "query": query, "read_failures": failures},
            )

        # A multi-message inbox read is a summary operation. Full HTML/rich bodies
        # are expensive and frequently drown out the useful metadata in the model
        # context. A single-message read explicitly opts into the bounded body.
        for item in parsed:
            if isinstance(item, dict) and item.get("snippet"):
                item["snippet"] = _compact_email_body(item.get("snippet"), limit=600)
            if max_results > 1 and isinstance(item, dict):
                item.pop("body", None)

        summary_mode = max_results > 1
        human = (
            f"Read {len(parsed)} email{'s' if len(parsed) != 1 else ''} matching '{query}'. "
            + ("These are summaries; read one message at a time for the full body." if summary_mode else "The message body is included.")
        )
        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=parsed,
            human_readable=human,
            verified=True,
            verification_status="verified",
            verification_details={"count": len(parsed), "matched_count": len(rows), "query": query, "read_failures": len(failures)},
            metadata={"count": len(parsed), "matched_count": len(rows), "query": query, "read_failures": failures},
        )
