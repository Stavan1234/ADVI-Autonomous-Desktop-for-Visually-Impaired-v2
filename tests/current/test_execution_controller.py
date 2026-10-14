from __future__ import annotations

from advi.agent.contracts import ExecutionRoute, TaskState
from advi.core.execution_controller import ExecutionController
from advi.tools.contracts import ToolCall, ToolResult


def verified_result(name: str, *, output=None, metadata=None, verification=None) -> ToolResult:
    return ToolResult(
        call_id=f"r-{name}",
        name=name,
        success=True,
        verified=True,
        verification_status="verified",
        output=output,
        metadata=metadata or {},
        verification_details=verification or {},
        human_readable="ok",
    )


def test_navigate_continues_to_explicit_search_query() -> None:
    task = TaskState(goal="Go to Amazon and search for \"2TB SSD\"", task_id="t1")
    task.protect_parameters({"query": "2TB SSD"}, operation_intent="web_search")
    result = verified_result("navigate", metadata={"current_url": "https://www.amazon.in/"})

    continuation = ExecutionController().propose(
        task,
        ToolCall("nav", "navigate", {"url": "https://www.amazon.in/"}),
        result,
        available_tools={"navigate", "search"},
    )

    assert continuation is not None
    assert continuation.call.name == "search"
    assert continuation.call.arguments == {"query": "2TB SSD"}


def test_navigate_does_not_search_without_explicit_query() -> None:
    task = TaskState(goal="Go to Amazon", task_id="t2")
    result = verified_result("navigate", metadata={"current_url": "https://www.amazon.in/"})

    assert ExecutionController().propose(
        task,
        ToolCall("nav", "navigate", {"url": "https://www.amazon.in/"}),
        result,
        available_tools={"navigate", "search"},
    ) is None


def test_unique_find_result_continues_to_open_file() -> None:
    task = TaskState(goal="Open Music Day Speech.pdf", task_id="t3")
    result = verified_result("find_file", output=["C:/Users/Test/Downloads/Music Day Speech.pdf"])

    continuation = ExecutionController().propose(
        task,
        ToolCall("find", "find_file", {"name": "Music Day Speech.pdf"}),
        result,
        available_tools={"find_file", "open_file"},
    )

    assert continuation is not None
    assert continuation.call.name == "open_file"
    assert continuation.call.arguments["path"].endswith("Music Day Speech.pdf")


def test_unique_find_result_continues_to_read_file_for_read_request() -> None:
    task = TaskState(goal="Read Music Day Speech.pdf", task_id="t4")
    result = verified_result("find_file", output=["C:/Users/Test/Downloads/Music Day Speech.pdf"])

    continuation = ExecutionController().propose(
        task,
        ToolCall("find", "find_file", {"name": "Music Day Speech.pdf"}),
        result,
        available_tools={"find_file", "read_file"},
    )

    assert continuation is not None
    assert continuation.call.name == "read_file"




def test_unique_find_result_continues_to_open_folder_for_folder_request() -> None:
    task = TaskState(goal="Open the Projects folder", task_id="t4b")
    result = verified_result("find_file", output=["C:/Users/Test/Projects"])

    continuation = ExecutionController().propose(
        task,
        ToolCall("find", "find_file", {"name": "Projects"}),
        result,
        available_tools={"find_file", "open_folder", "open_file"},
    )

    assert continuation is not None
    assert continuation.call.name == "open_folder"

def test_multiple_find_results_are_not_guessed() -> None:
    task = TaskState(goal="Open Music Day Speech.pdf", task_id="t5")
    result = verified_result("find_file", output=["C:/A/Music Day Speech.pdf", "C:/B/Music Day Speech.pdf"])

    assert ExecutionController().propose(
        task,
        ToolCall("find", "find_file", {"name": "Music Day Speech.pdf"}),
        result,
        available_tools={"find_file", "open_file"},
    ) is None


def test_explicit_first_result_can_continue_from_verified_browser_candidates() -> None:
    task = TaskState(goal="Search YouTube and play the first result", task_id="t6")
    result = verified_result(
        "search",
        output="worship music",
        verification={"result_candidates": [{"selector": "a#first-result"}]},
    )

    continuation = ExecutionController().propose(
        task,
        ToolCall("search", "search", {"query": "worship music"}),
        result,
        available_tools={"search", "click_web_element"},
    )

    assert continuation is not None
    assert continuation.call.name == "click_web_element"
    assert continuation.call.arguments == {"selector": "a#first-result"}


def test_controller_never_switches_a_david_task_to_native() -> None:
    task = TaskState(goal="Use David to open the file", task_id="t7")
    task.execution_route = ExecutionRoute.DAVID.value
    result = verified_result("find_file", output=["C:/A/file.pdf"])

    assert ExecutionController().propose(
        task,
        ToolCall("find", "find_file", {"name": "file.pdf"}),
        result,
        available_tools={"find_file", "open_file"},
    ) is None
