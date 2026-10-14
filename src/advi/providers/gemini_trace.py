from __future__ import annotations

from typing import Any


def gemini_response_to_trace_dict(response: Any) -> dict[str, Any]:
    """Extract traceable metadata from a raw Gemini GenerateContentResponse."""
    data: dict[str, Any] = {
        "response_type": type(response).__name__,
        "response_id": getattr(response, "response_id", None),
        "model_version": getattr(response, "model_version", None),
    }

    text = getattr(response, "text", None)
    data["raw_text"] = text if text is not None else ""
    data["raw_text_empty"] = not bool(text and str(text).strip())

    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        data["usage"] = {
            "prompt_token_count": getattr(
                usage,
                "prompt_token_count",
                None,
            ),
            "candidates_token_count": getattr(
                usage,
                "candidates_token_count",
                None,
            ),
            "total_token_count": getattr(
                usage,
                "total_token_count",
                None,
            ),
            "cached_content_token_count": getattr(
                usage,
                "cached_content_token_count",
                None,
            ),
        }

    candidates = getattr(response, "candidates", None) or []
    data["candidate_count"] = len(candidates)
    data["candidates"] = [
        _candidate_to_dict(candidate)
        for candidate in candidates
    ]

    prompt_feedback = getattr(response, "prompt_feedback", None)
    if prompt_feedback is not None:
        data["prompt_feedback"] = str(prompt_feedback)

    return data


def _candidate_to_dict(candidate: Any) -> dict[str, Any]:
    finish_reason = getattr(candidate, "finish_reason", None)
    if finish_reason is not None:
        finish_reason = (
            getattr(finish_reason, "name", None)
            or str(finish_reason)
        )

    parts: list[Any] = []
    content = getattr(candidate, "content", None)
    if content is not None:
        for part in getattr(content, "parts", []) or []:
            part_dict: dict[str, Any] = {}
            text = getattr(part, "text", None)
            if text is not None:
                part_dict["text"] = text
            if hasattr(part, "function_call"):
                part_dict["function_call"] = str(
                    getattr(part, "function_call", None)
                )
            if hasattr(part, "executable_code"):
                part_dict["executable_code"] = str(
                    getattr(part, "executable_code", None)
                )
            if part_dict:
                parts.append(part_dict)

    return {
        "finish_reason": finish_reason,
        "index": getattr(candidate, "index", None),
        "parts": parts,
        "safety_ratings": str(
            getattr(candidate, "safety_ratings", None)
        ),
    }


def gemini_config_to_trace_dict(config: Any) -> dict[str, Any]:
    """Serialize GenerateContentConfig fields for trace logging."""
    if config is None:
        return {}

    data: dict[str, Any] = {
        "response_mime_type": getattr(
            config,
            "response_mime_type",
            None,
        ),
        "max_output_tokens": getattr(
            config,
            "max_output_tokens",
            None,
        ),
        "system_instruction": _system_instruction_text(
            getattr(config, "system_instruction", None)
        ),
    }

    schema = getattr(config, "response_json_schema", None)
    if schema is not None:
        data["response_json_schema"] = schema

    thinking = getattr(config, "thinking_config", None)
    if thinking is not None:
        data["thinking_config"] = {
            "thinking_level": getattr(
                thinking,
                "thinking_level",
                None,
            ),
            "include_thoughts": getattr(
                thinking,
                "include_thoughts",
                None,
            ),
        }

    tools = getattr(config, "tools", None)
    data["tools_configured"] = bool(tools)
    data["tools_count"] = len(tools) if tools else 0

    tool_config = getattr(config, "tool_config", None)
    if tool_config is not None:
        afc = getattr(
            tool_config,
            "automatic_function_calling",
            None,
        )
        if afc is not None:
            data["automatic_function_calling"] = {
                "disable": getattr(afc, "disable", None),
                "maximum_remote_calls": getattr(
                    afc,
                    "maximum_remote_calls",
                    None,
                ),
            }

    return data


def _system_instruction_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    parts = getattr(value, "parts", None)
    if parts:
        texts = []
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                texts.append(text)
        if texts:
            return "\n".join(texts)
    return str(value)


def gemini_contents_to_trace_list(
    contents: list[Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for content in contents:
        role = getattr(content, "role", None)
        parts_text: list[str] = []
        for part in getattr(content, "parts", []) or []:
            text = getattr(part, "text", None)
            if text is not None:
                parts_text.append(text)
        rows.append(
            {
                "role": role,
                "parts": parts_text,
            }
        )
    return rows
