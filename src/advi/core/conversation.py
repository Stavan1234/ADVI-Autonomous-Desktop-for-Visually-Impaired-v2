from __future__ import annotations

from dataclasses import dataclass, field

from ..io.output import AdviResponse
from ..memory.retriever import MemoryRetriever
from ..memory.session import SessionBuffer
from ..memory.short_term import ShortTermMemory
from ..providers import LLMProvider


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
        if system_parts:
            request_messages.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        "You are ADVI, an Autonomous Desktop "
                        "for the Visually Impaired.\n\n"
                        "Answer clearly, naturally, and "
                        "appropriately for speech.\n\n"
                        "You have been given memory and "
                        "relationship context from previous "
                        "interactions.\n\n"
                        "Use the context when it is relevant "
                        "to the user's current question.\n"
                        "Ignore unrelated context.\n"
                        "Do not mention the memory system.\n"
                        "Do not claim you do not know something "
                        "when the supplied context contains "
                        "the answer.\n\n"
                        + "\n\n".join(system_parts)
                    ),
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