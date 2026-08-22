from __future__ import annotations

from dataclasses import dataclass, field

from ..io.output import AdviResponse
from ..memory.retriever import MemoryRetriever
from ..memory.session import SessionBuffer
from ..memory.short_term import ShortTermMemory
from ..providers import LLMProvider
from .personality import build_advi_system_prompt
from .response_policy import build_response_policy
from .capabilities import capability_for_prompt


@dataclass
class ConversationEngine:
    provider: LLMProvider
    memory: ShortTermMemory = field(
        default_factory=ShortTermMemory
    )
    session_buffer: SessionBuffer = field(
        default_factory=SessionBuffer
    )
    retriever: MemoryRetriever | None = None
    previous_session_context: str | None = None

    def respond(
        self,
        user_input: str,
    ) -> AdviResponse:
        text = user_input.strip()

        if not text:
            return AdviResponse("")

        evicted = self.memory.add(
            "user",
            text,
        )

        self.session_buffer.add(evicted)

        messages = self.memory.get_messages()
        request_messages = list(messages)

        system_parts: list[str] = []

        system_parts.append(
            capability_for_prompt()
        )

        # ---------------------------------------------------------
        # 1. Normal long-term memory retrieval
        # ---------------------------------------------------------
        if self.retriever is not None:
            retrieved = self.retriever.search(
                text,
                limit=12,
            )

            if retrieved:
                memory_context = "\n".join(
                    f"- {item.memory.statement}"
                    for item in retrieved
                )

                system_parts.append(
                    "Relevant long-term memories:\n"
                    + memory_context
                )

            # -----------------------------------------------------
            # 2. Relationship retrieval
            # -----------------------------------------------------
            relationship_rows = (
                self.retriever.memory
                .search_relationships(
                    text,
                    limit=12,
                )
            )

            if relationship_rows:
                relationship_context = "\n".join(
                    (
                        f"- {item.subject} "
                        f"{item.relation} "
                        f"{item.object}"
                    )
                    for item in relationship_rows
                )

                system_parts.append(
                    "Relevant relationship facts:\n"
                    + relationship_context
                )

        # ---------------------------------------------------------
        # 3. Previous session summaries
        # ---------------------------------------------------------
        if self.previous_session_context:
            system_parts.append(
                "Recent session context:\n"
                + self.previous_session_context
            )

        # ---------------------------------------------------------
        # 4. Build system context
        # ---------------------------------------------------------
        system_content = build_advi_system_prompt()

        system_content += (
            "\n\n"
            + build_response_policy(text)
        )

        if system_parts:
            system_content += (
                "\n\nContext available for this request:\n"
                + "\n\n".join(system_parts)
                + "\n\nUse this context only when relevant."
            )

        request_messages.insert(
            0,
            {
                "role": "system",
                "content": system_content,
            },
        )

        # ---------------------------------------------------------
        # 5. Main LLM call
        # ---------------------------------------------------------
        result = self.provider.chat(
            request_messages
        )

        # ---------------------------------------------------------
        # 6. Store assistant response in short-term memory
        # ---------------------------------------------------------
        evicted = self.memory.add(
            "assistant",
            result.text,
        )

        self.session_buffer.add(evicted)

        return AdviResponse(result.text)

    def get_session_buffer(
        self,
    ) -> list[dict[str, str]]:
        return [
            message.copy()
            for message in self.session_buffer.messages
        ] + self.memory.get_messages()