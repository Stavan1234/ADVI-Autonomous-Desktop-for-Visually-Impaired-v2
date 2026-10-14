from __future__ import annotations

from dataclasses import dataclass, field
import json

from ..providers import LLMProvider


@dataclass
class MemoryCandidate:
    category: str
    key: str
    value: str
    statement: str
    confidence: float = 0.8


@dataclass
class RelationshipCandidate:
    subject: str
    relation: str
    object: str
    confidence: float = 0.8


@dataclass
class ConsolidationResult:
    summary: str = ""
    memories: list[MemoryCandidate] = field(
        default_factory=list
    )
    relationships: list[RelationshipCandidate] = field(
        default_factory=list
    )


class SessionConsolidator:
    """Extract durable information from a completed session."""

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def consolidate(
        self,
        messages: list[dict[str, str]],
    ) -> ConsolidationResult:
        if not messages:
            return ConsolidationResult()

        recent = list(messages)[-40:]
        conversation = "\n".join(
            f"{message['role'].upper()}: {message['content']}"
            for message in recent
        )
        # Consolidation is background/optional work. Keep its input bounded so
        # it cannot turn a long interactive session into another huge provider
        # request during shutdown.
        if len(conversation) > 12000:
            conversation = conversation[-12000:]

        prompt = f"""

Memory evidence policy:

You may use the entire conversation, including USER and ASSISTANT
messages, to understand context and resolve incomplete or messy
user statements.

However, distinguish carefully between:

1. USER-STATED FACT
   A fact explicitly stated by the user.

2. USER-CONFIRMED FACT
   A fact proposed by ADVI and explicitly confirmed by the user.

3. ASSISTANT INFERENCE
   A fact introduced only by ADVI/Groq/Gemini without user
   confirmation.

4. UNCERTAIN INFORMATION
   Anything ambiguous or insufficiently supported.

Only store information as durable memory when it is:
- explicitly stated by the user, or
- explicitly confirmed by the user.

Assistant-generated wording may be used to interpret or clarify
the user's meaning, but assistant guesses must never become facts
solely because ADVI said them.

For names, email addresses, relationships, identities, jobs,
preferences, ownership, and similar personal facts, require
explicit user support or confirmation.        

Relationships are optional.

Extract a relationship only when the conversation
explicitly establishes it.

Examples:

"The user's father is Devdan Kalkumbe."
→
{{
  "subject": "Stavan Kalkumbe",
  "relation": "father",
  "object": "Devdan Kalkumbe",
  "confidence": 0.98
}}

"Pradeep is my uncle and Ramanika is his daughter."
→
{{
  "subject": "Pradeep",
  "relation": "daughter",
  "object": "Ramanika",
  "confidence": 0.98
}}

Do NOT infer relationships that were not explicitly
established.

Do not create relationships from guesses.        
Analyze this completed ADVI conversation.

Keep ONLY information that is likely to remain useful
across future sessions.

Do NOT store:
- temporary questions
- ordinary factual answers
- transient tasks
- debugging noise
- assumptions
- filler

Potentially store:
- stable user facts
- stable preferences
- important project decisions
- persistent context

For every memory, create a natural, self-contained
statement specifically useful for semantic retrieval.

Examples:

User says:
"I study at FCRIT Vashi."

Return:
{{
  "category": "user",
  "key": "institution",
  "value": "FCRIT Vashi",
  "statement": "The user studies at FCRIT Vashi.",
  "confidence": 0.98
}}

User says:
"I am a fourth year computer engineering student."

Return:
{{
  "category": "user",
  "key": "education",
  "value": "Fourth-year computer engineering student",
  "statement": "The user is a fourth-year computer engineering student.",
  "confidence": 0.98
}}

The statement must:
- be short
- be factual
- be self-contained
- contain enough meaning for semantic retrieval
- contain no assumptions

Return ONLY valid JSON:

{{
  "summary": "Very short session summary.",
  "memories": [
    {{
      "category": "user|preference|project",
      "key": "short_key",
      "value": "structured durable value",
      "statement": "natural factual statement",
      "confidence": 0.0
    }}
  ],
  "relationships": [
  {{
    "subject": "Stavan Kalkumbe",
    "relation": "father",
    "object": "Devdan Kalkumbe",
    "confidence": 0.98
  }}
]
}}

Conversation:

{conversation}
""".strip()

        result = self.provider.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are ADVI's memory consolidation "
                        "component. Extract only durable information "
                        "and produce precise retrieval statements."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            purpose="consolidation",
        )

        return self._parse(result.text)

    @staticmethod
    def _parse(text: str) -> ConsolidationResult:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return ConsolidationResult()

        summary = str(
            data.get("summary", "")
        ).strip()

        memories: list[MemoryCandidate] = []

        for item in data.get("memories", []):
            if not isinstance(item, dict):
                continue

            category = str(
                item.get("category", "")
            ).strip()

            key = str(
                item.get("key", "")
            ).strip()

            value = str(
                item.get("value", "")
            ).strip()

            statement = str(
                item.get("statement", "")
            ).strip()

            try:
                confidence = float(
                    item.get("confidence", 0.8)
                )
            except (TypeError, ValueError):
                confidence = 0.8

            confidence = max(
                0.0,
                min(1.0, confidence),
            )

            if (
                category
                and key
                and value
                and statement
            ):
                memories.append(
                    MemoryCandidate(
                        category=category,
                        key=key,
                        value=value,
                        statement=statement,
                        confidence=confidence,
                    )
                )

        relationships: list[RelationshipCandidate] = []

        for item in data.get(
            "relationships",
            [],
        ):
            if not isinstance(item, dict):
                continue

            subject = str(
                item.get("subject", "")
            ).strip()

            relation = str(
                item.get("relation", "")
            ).strip()

            object_value = str(
                item.get("object", "")
            ).strip()

            try:
                confidence = float(
                    item.get("confidence", 0.8)
                )
            except (TypeError, ValueError):
                confidence = 0.8

            confidence = max(
                0.0,
                min(1.0, confidence),
            )

            if (
                subject
                and relation
                and object_value
            ):
                relationships.append(
                    RelationshipCandidate(
                        subject=subject,
                        relation=relation,
                        object=object_value,
                        confidence=confidence,
                    )
                )

        return ConsolidationResult(
            summary=summary,
            memories=memories,
            relationships=relationships,
        )