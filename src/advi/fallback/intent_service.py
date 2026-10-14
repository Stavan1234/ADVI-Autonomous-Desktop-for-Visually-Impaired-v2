from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..providers import LLMProvider
from .intent_schema import Intent


logger = logging.getLogger(__name__)


class IntentService:
    """Extract an agentic desktop intent from user input."""

    def __init__(
        self,
        provider: LLMProvider,
        prompt_path: str | Path,
    ) -> None:
        self.provider = provider

        prompt_file = Path(prompt_path)

        with prompt_file.open(
            "r",
            encoding="utf-8",
        ) as file:
            self.prompt = file.read()

    @staticmethod
    def _deterministic_web_search_intent(user_input: str) -> Intent | None:
        """Preserve an explicit web-search literal instead of letting an LLM paraphrase it."""
        text = str(user_input or "").strip()
        if not re.search(r"\bsearch\s+(?:for|the web for|online for)\b", text, re.I):
            return None
        match = re.search(r"\bsearch\s+(?:for|the web for|online for)\s+[\"“](.*?)[\"”]", text, re.I)
        if not match:
            match = re.search(r"\bsearch\s+(?:for|the web for|online for)\s+(.+?)\s*[.!?]?$", text, re.I)
        if not match:
            return None
        query = match.group(1).strip().strip('\"“”\'')
        if not query:
            return None
        return Intent(goal="web_search", entities={"query": query}, constraints=[], confirmation_required=False, missing_information=[])

    @staticmethod
    def _deterministic_email_intent(user_input: str) -> Intent | None:
        """Parse complete explicit send-email requests without invoking an LLM."""
        text = str(user_input or "").strip()
        if not re.search(r"\b(?:send|email|mail)\b", text, re.I):
            return None
        email = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I)
        if not email:
            return None
        recipient=email.group(0)
        subject=""
        m=re.search(r"\bsubject\s*(?::|is|=|to)?\s*[\"“](.*?)[\"”]",text,re.I)
        if m:
            subject=m.group(1).strip()
        else:
            m=re.search(r"\bsubject\s*(?::|is|=|to)?\s*(.*?)\s+\b(?:and\s+)?(?:body|message|saying|say)\b",text,re.I)
            if m: subject=m.group(1).strip()
        body=""
        m=re.search(r"\b(?:body|message|saying|say)\s*(?::|is|=|to)?\s*[\"“](.*?)[\"”]\s*[.!?]?\s*$",text,re.I)
        if m:
            body=m.group(1).strip()
        else:
            m=re.search(r"\b(?:body|message|saying|say)\s*(?::|is|=|to)?\s*(.+)$",text,re.I)
            if m: body=m.group(1).strip()
        if not body:
            return None
        return Intent(goal="send_email", entities={"recipient_email":recipient,"subject":subject,"content":body}, constraints=[], confirmation_required=True, missing_information=[])

    def extract_intent(
        self,
        user_input: str,
    ) -> Intent:
        deterministic_search = self._deterministic_web_search_intent(user_input)
        if deterministic_search is not None:
            return deterministic_search

        deterministic = self._deterministic_email_intent(user_input)
        if deterministic is not None:
            return deterministic

        full_prompt = (
            f"{self.prompt}\n\n"
            "User Input:\n\n"
            f"{user_input}"
        )

        result = self.provider.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are ADVI's agentic desktop "
                        "intent extraction component. "
                        "Return only valid JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": full_prompt,
                },
            ]
        )

        response = result.text.strip()

        intent_dict = json.loads(response)

        return Intent(
            **intent_dict
        )