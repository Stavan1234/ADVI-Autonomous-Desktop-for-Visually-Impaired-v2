from __future__ import annotations

import logging
import re
from typing import Any

from advi.tools.contracts import ToolCall, ToolResult
from advi.memory.long_term import LongTermMemory
from advi.memory.retriever import MemoryRetriever

logger = logging.getLogger(__name__)


class MemoryCapability:
    """
    Durable Memory Capability.
    Provides verified persistence, semantic search, and evidence retrieval over user facts
    via LongTermMemory (SQLite) and MemoryRetriever.
    """

    SUPPORTED_ACTIONS = {
        "memory_retrieval",
        "contact_lookup",
        "memory_update",
        "memory_forget",
    }

    def __init__(
        self,
        memory: LongTermMemory | None = None,
        retriever: MemoryRetriever | None = None,
    ) -> None:
        self.memory = memory
        self.retriever = retriever

    def is_available(self) -> bool:
        return self.memory is not None or self.retriever is not None

    def execute(self, action: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        handler = getattr(self, f"_execute_{action.action}", None)
        if handler is None:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=f"Unsupported memory action: {action.action}",
            )

        try:
            return handler(action)
        except Exception as exc:
            logger.exception("Memory action failed: %s", action.action)
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=str(exc),
            )

    def _execute_memory_retrieval(self, action: ToolCall) -> ToolResult:
        query = str(action.parameters.get("query") or action.target or "").strip()
        if not query:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No query provided for memory retrieval.",
            )

        facts: list[str] = []

        # 1. Try hybrid semantic retrieval
        if self.retriever:
            try:
                retrieved = self.retriever.retrieve(query, limit=5)
                if retrieved:
                    facts.extend(retrieved)
            except Exception as exc:
                logger.warning("MemoryRetriever error: %s", exc)

        # 2. Fallback to direct SQLite lexical search if retriever empty or missing
        if not facts and self.memory:
            try:
                matches = self.memory.search(query, limit=5)
                facts = [m.statement for m in matches if m.statement]
            except Exception as exc:
                logger.warning("LongTermMemory search error: %s", exc)

        human_readable = (
            f"Found {len(facts)} relevant facts in memory."
            if facts
            else "No matching facts found in memory."
        )

        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=facts,
            human_readable=human_readable,
            verified=True,
            metadata={"query": query, "count": len(facts)},
        )

    def _execute_contact_lookup(self, action: ToolCall) -> ToolResult:
        name = str(
            action.parameters.get("name")
            or action.parameters.get("query")
            or action.target
            or ""
        ).strip()
        if not name:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No contact name provided for contact lookup.",
                output=[],
                verified=False,
            )

        if not self.memory:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="Long-term contact store is not available.",
                output=[],
                verified=False,
            )

        try:
            # 1. First attempt exact match
            exact_contact = self.memory.get_contact(name)
            if exact_contact is not None:
                contact_dict = {
                    "id": exact_contact.id,
                    "name": exact_contact.name,
                    "email": exact_contact.email,
                }
                return ToolResult(
                    call_id=action.call_id,
                    name=action.action,
                    success=True,
                    output=[contact_dict],
                    human_readable=f"Found contact {exact_contact.name} with email {exact_contact.email}.",
                    verified=True,
                    metadata={"query": name, "count": 1, "match": "exact"},
                )

            # 2. If no exact match, bounded candidate search.
            matches = self.memory.find_contacts(name, limit=5)
            if not matches:
                # Older sessions could have consolidated a contact into the generic
                # durable-memory table without populating the dedicated contacts table.
                # Recover that trusted fact deterministically and backfill the contact
                # table so future lookups are O(1) and do not depend on consolidation.
                try:
                    generic_matches = self.memory.search(name, limit=8)
                except Exception:
                    generic_matches = []
                email_pattern = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I)
                recovered: list[tuple[str, str]] = []
                for memory_item in generic_matches:
                    source = " ".join((str(getattr(memory_item, "statement", "")), str(getattr(memory_item, "value", ""))))
                    emails = []
                    seen_emails = set()
                    for email in email_pattern.findall(source):
                        normalized = email.casefold()
                        if normalized not in seen_emails:
                            seen_emails.add(normalized)
                            emails.append(email)
                    for email in emails:
                        recovered.append((name, email))
                unique_recovered = list(dict.fromkeys((n.casefold(), e.casefold(), n, e) for n, e in recovered))
                if len(unique_recovered) == 1:
                    _, _, recovered_name, recovered_email = unique_recovered[0]
                    self.memory.save_contact(recovered_name, recovered_email)
                    return ToolResult(
                        call_id=action.call_id,
                        name=action.action,
                        success=True,
                        output=[{"name": recovered_name, "email": recovered_email}],
                        human_readable=f"Recovered trusted contact {recovered_name} with email {recovered_email} from durable memory and restored the contact entry.",
                        verified=True,
                        metadata={"query": name, "count": 1, "match": "memory_backfill", "source": "durable_memory"},
                    )
                if len(unique_recovered) > 1:
                    candidates = [{"name": row[2], "email": row[3]} for row in unique_recovered[:5]]
                    names_and_emails = ", ".join(f"{row['name']} ({row['email']})" for row in candidates)
                    return ToolResult(
                        call_id=action.call_id,
                        name=action.action,
                        success=True,
                        output=candidates,
                        human_readable=f"Multiple trusted memory contacts matched '{name}': {names_and_emails}. Clarification required.",
                        verified=True,
                        metadata={"query": name, "count": len(candidates), "ambiguous": True, "needs_user_input": True, "waiting_for": "which matching contact to use", "source": "durable_memory"},
                    )
                return ToolResult(
                    call_id=action.call_id,
                    name=action.action,
                    success=True,
                    output=[],
                    human_readable=f"No contact found for '{name}'.",
                    verified=True,
                    metadata={"query": name, "count": 0, "needs_user_input": True, "waiting_for": "the email address for this contact"},
                )

            contact_dicts = [
                {"id": c.id, "name": c.name, "email": c.email}
                for c in matches
            ]
            if len(contact_dicts) == 1:
                single = contact_dicts[0]
                return ToolResult(
                    call_id=action.call_id,
                    name=action.action,
                    success=True,
                    output=contact_dicts,
                    human_readable=f"Found contact {single['name']} with email {single['email']}.",
                    verified=True,
                    metadata={"query": name, "count": 1, "match": "single_fuzzy"},
                )

            # Multiple candidates found -> Ambiguity
            names_and_emails = ", ".join(f"{c['name']} ({c['email']})" for c in contact_dicts)
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=True,
                output=contact_dicts,
                human_readable=f"Multiple contacts matched '{name}': {names_and_emails}. Clarification required.",
                verified=True,
                metadata={"query": name, "count": len(contact_dicts), "ambiguous": True, "needs_user_input": True, "waiting_for": "which matching contact to use"},
            )
        except Exception as exc:
            logger.exception("Error executing contact lookup for '%s': %s", name, exc)
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=f"Contact lookup failed: {exc}",
                output=[],
                verified=False,
            )

    def _execute_memory_update(self, action: ToolCall) -> ToolResult:
        fact = str(
            action.parameters.get("fact")
            or action.parameters.get("content")
            or action.parameters.get("value")
            or action.target
            or ""
        ).strip()

        if not fact:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No fact provided to store in memory.",
            )

        if not self.memory:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="Long-term SQLite memory store is not available.",
            )

        category = str(action.parameters.get("category") or "user_fact").strip()
        key = str(action.parameters.get("key") or "").strip()

        # Derive a clean key if not explicitly given
        if not key:
            key_raw = re.sub(r"[^a-zA-Z0-9]+", "_", fact[:35]).strip("_").lower()
            key = key_raw or "general_fact"

        value = str(action.parameters.get("value") or fact).strip()

        # 1. Perform actual write to SQLite
        try:
            self.memory.remember(
                category=category,
                key=key,
                value=value,
                statement=fact,
            )
        except Exception as exc:
            logger.exception("Failed to write memory to SQLite: %s", exc)
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=f"Database write error: {exc}",
            )

        # 2. Verify write actually occurred in SQLite
        verified_memory = self.memory.recall(category=category, key=key)
        if not verified_memory:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="Memory write failed verification; record not found in database.",
                verified=False,
            )

        # 3. Refresh retriever cache so new memory is immediately retrievable
        if self.retriever:
            try:
                self.retriever.refresh()
            except Exception as exc:
                logger.warning("Could not refresh retriever cache: %s", exc)

        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output={"category": category, "key": key, "statement": fact},
            human_readable=f"Remembered: '{fact}'.",
            verified=True,
            verification_details={"id": verified_memory.id, "category": category, "key": key},
            metadata={"category": category, "key": key},
        )

    def _execute_memory_forget(self, action: ToolCall) -> ToolResult:
        target = str(
            action.parameters.get("fact")
            or action.parameters.get("key")
            or action.target
            or ""
        ).strip()

        if not target:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="No target fact or key specified to forget.",
            )

        if not self.memory:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error="Long-term SQLite memory store is not available.",
            )

        category = str(action.parameters.get("category") or "").strip()
        key = str(action.parameters.get("key") or "").strip()

        deleted_count = 0

        # If category and key are explicitly provided
        if category and key:
            self.memory.forget(category=category, key=key)
            deleted_count = 1
        else:
            # Search for matching memories to forget
            matches = self.memory.search(target, limit=5)
            for m in matches:
                self.memory.forget(category=m.category, key=m.key)
                deleted_count += 1

        if deleted_count == 0:
            return ToolResult(
                call_id=action.call_id,
                name=action.action,
                success=False,
                error=f"No matching memory found to forget for target '{target}'.",
                verified=False,
            )

        # Refresh retriever cache
        if self.retriever:
            try:
                self.retriever.refresh()
            except Exception:
                pass

        return ToolResult(
            call_id=action.call_id,
            name=action.action,
            success=True,
            output=target,
            human_readable=f"Removed {deleted_count} record(s) matching '{target}' from memory.",
            verified=True,
            metadata={"deleted_count": deleted_count},
        )
