from __future__ import annotations

import json

from advi.core.context_router import ContextPacket, ContextRouter


def test_context_packet_never_corrupts_json_by_character_cutoff():
    value = "important-data-" * 1000
    packet = ContextPacket({"observation": {"value": value}})

    text = packet.as_text()
    payload = text.split("\n", 1)[1]
    decoded = json.loads(payload)

    assert decoded["observation"]["value"] == value
    assert packet.serialized_chars == len(payload)


def test_router_contains_only_explicit_dynamic_sections():
    packet = ContextRouter().build(
        task={"goal": "open browser"},
        memories=["user prefers Chrome"],
        observation={"active_window": "Chrome"},
    )

    assert set(packet.sections) == {"task", "recalled_memory", "observation"}
    assert "transcript" not in packet.sections
    assert "recent_history" not in packet.sections
    assert "messages" not in packet.sections


def test_router_limits_memory_by_item_count_without_rewriting_values():
    memories = [f"memory-{i}-" + ("x" * 500) for i in range(8)]
    packet = ContextRouter(max_memory_items=5).build(memories=memories)

    assert packet.sections["recalled_memory"] == memories[:5]


def test_router_bounds_task_and_observation_without_corrupting_structure():
    task = {"goal": "do the exact thing", "details": {"command": "keep-this-verbatim"}}
    observation = {"screen_text": "KEEP THIS WHOLE" * 100}

    packet = ContextRouter().build(task=task, observation=observation)

    assert packet.sections["task"]["goal"] == task["goal"]
    assert packet.sections["task"]["details"] == task["details"]
    assert packet.serialized_chars <= 7000
    assert packet.sections["observation"]["screen_text"].startswith("KEEP THIS WHOLE")
