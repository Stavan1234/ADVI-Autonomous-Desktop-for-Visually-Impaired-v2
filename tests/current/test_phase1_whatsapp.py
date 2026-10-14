"""WhatsApp skill against a fake UIA tree shaped like WhatsApp Desktop (WebView2).

Shapes come from a read-only probe of the real app: one Edit named
"Search or start a new chat" with ValuePattern, a DataGrid named "Chat list"
whose DataItem rows concatenate title/time/preview, nested cells exposing the
title, and a second Edit (the composer) once a chat is open.
"""
from __future__ import annotations

from types import SimpleNamespace

from advi.agent import AgentSession
from advi.capabilities.apps import MessagingCapability
from advi.capabilities.apps.uia import UiaNode
from advi.capabilities.apps.whatsapp import SEARCH_BOX_NAME, WhatsAppSkill
from advi.capabilities.registry import Capability, CapabilityRegistry, CapabilityStatus
from advi.providers.base import LLMProvider, LLMResponse, LLMToolCall
from advi.tools import ToolCall, ToolGuard, ToolRegistry, ToolRunner
from advi.tools.catalog import REQUEST_TOOLS_NAME
from advi.tools.contracts import ToolSpec


def _node(name, control_type, rect=(0, 0, 10, 10), automation_id=""):
    return UiaNode(raw=None, name=name, control_type=control_type, automation_id=automation_id, rect=rect, hwnd=1)


class FakeWhatsApp:
    """Stateful fake; nodes are re-created per query like real UIA snapshots.

    Behaviour mirrors live probes (2026-10-14):
    - the search box is named "Search or start a new chat" only while empty;
    - cells are "<title> <date>"; the composer is "Type a message to [group] <title>";
    - injected keystrokes never reach the page (WebView2 visual hosting), while
      WM_CHAR posted to the host does; the composer ignores SetValue;
    - the empty composer reads "\n"; a "Send" button appears once it has text.
    """

    def __init__(self, chats, *, running=True, logged_in=True, focus_works=True,
                 composer_readable=True, drafts=None, groups=(), drop_posts=0,
                 search_prefill="", set_value_works=True, keyboard_works=False, post_works=True):
        self.chats = list(chats)
        self.groups = set(groups)
        self.running = running
        self.logged_in = logged_in
        self.focus_works = focus_works
        self.composer_readable = composer_readable
        self.drafts = dict(drafts or {})
        self.drop_posts = drop_posts
        self.set_value_works = set_value_works
        self.keyboard_works = keyboard_works
        self.post_works = post_works
        self.values = {"search": search_prefill, "composer": "\n"}
        self.focused = None
        self.open_title = None
        self.sent: list[tuple[str, str]] = []
        self.launched = []
        self.clicked: list[str] = []
        self.window = _node("WhatsApp", "Window")
        self.doc = _node("(3) WhatsApp", "Document", automation_id="RootWebArea")

    # --- node factories ---
    def _search(self):
        name = SEARCH_BOX_NAME if not self.values["search"] else ""
        return _node(name, "Edit", rect=(10, 10, 290, 30), automation_id="search")

    def _composer_node(self):
        kind = "group " if self.open_title in self.groups else ""
        return _node(f"Type a message to {kind}{self.open_title}", "Edit", rect=(320, 760, 900, 790), automation_id="composer")

    def _grid(self):
        return _node("Search results." if self.values["search"] else "Chat list", "DataGrid",
                     rect=(0, 40, 300, 800), automation_id="grid")

    def _visible_titles(self):
        query = self.values["search"].casefold()
        return [c for c in self.chats if query in c.casefold()] if query else list(self.chats)

    @staticmethod
    def _role(node):
        return node.automation_id

    def _append(self, text):
        if self.focused not in self.values:
            return
        current = self.values[self.focused]
        self.values[self.focused] = ("" if current == "\n" else current) + text

    # --- UiaDriver ---
    def find_window(self, title_contains, process_names):
        return self.window if self.running else None

    def bring_to_front(self, window):
        return True

    def launch(self, target):
        self.launched.append(target)
        self.running = True

    def find_first(self, root, *, control_type=None, name=None, automation_id=None):
        if root is self.window and control_type == "Document":
            return self.doc
        if not self.logged_in:
            return None
        if control_type == "Edit" and name == SEARCH_BOX_NAME:
            search = self._search()
            return search if search.name == name else None
        if control_type == "DataGrid":
            grid = self._grid()
            return grid if grid.name == name else None
        if control_type == "Button" and name == "Send" and self.open_title and self.values["composer"].strip():
            return _node("Send", "Button", rect=(1200, 760, 1240, 790), automation_id="send")
        return None

    def find_all(self, root, *, control_type=None, name=None):
        nodes = []
        if self.logged_in:
            nodes.append(self._search())
        if self.open_title is not None:
            nodes.append(self._composer_node())
        return [n for n in nodes if (control_type is None or n.control_type == control_type) and (name is None or n.name == name)]

    def children(self, node):
        role = node.automation_id
        if role == "grid":
            return [_node(f"{t} 21/07/2025 You : hi", "DataItem", rect=(0, 50, 300, 110), automation_id=f"row:{t}")
                    for t in self._visible_titles()]
        if role.startswith("row:"):
            title = role[4:]
            return [_node(f"{title} 21/07/2025", "DataItem", automation_id=f"cell:{title}"),
                    _node("21/07/2025", "DataItem", automation_id="stamp")]
        return []

    def has_focus(self, node):
        return self.focused == self._role(node)

    def focus(self, node):
        if not self.focus_works:
            return False
        self.focused = self._role(node)
        return True

    def value(self, node):
        role = self._role(node)
        if role == "composer" and not self.composer_readable:
            return None
        return self.values.get(role)

    def set_value(self, node, text):
        if not self.set_value_works:
            return False
        if self._role(node) == "search":
            self.values["search"] = text
        return True  # the rich-text composer reports success but ignores it

    def invoke(self, node):
        if node.automation_id == "send" and self.values["composer"].strip():
            self.sent.append((self.open_title, self.values["composer"]))
            self.values["composer"] = "\n"
            return True
        return False

    def post_text(self, window, text):
        if not self.post_works:
            return False
        if self.drop_posts:
            self.drop_posts -= 1
            return True
        self._append(text)
        return True

    def post_backspaces(self, window, count):
        if self.focused in self.values:
            text = self.values[self.focused].rstrip("\n")
            text = text[: max(0, len(text) - count)]
            self.values[self.focused] = text or ("\n" if self.focused == "composer" else "")
        return True

    def click(self, node):
        role = self._role(node)
        if role.startswith("row:"):
            self.clicked.append(role[4:])
            self.open_title = role[4:]
            self.values["composer"] = self.drafts.get(self.open_title, "\n")
        elif role in ("search", "composer") and self.focus_works:
            self.focus(node)

    def type_text(self, text):
        if self.keyboard_works:
            self._append(text)

    def press(self, keys):
        if not self.keyboard_works or self.focused not in self.values:
            return
        if keys == "^a{BACKSPACE}":
            self.values[self.focused] = "\n" if self.focused == "composer" else ""
        elif keys == "{ENTER}" and self.focused == "composer" and self.values["composer"].strip():
            self.sent.append((self.open_title, self.values["composer"]))
            self.values["composer"] = "\n"


def _skill(app):
    clock = iter(range(0, 10_000))
    return WhatsAppSkill(app, sleep=lambda _s: None, clock=lambda: next(clock), step_timeout=3, launch_timeout=3)


# --- skill flow ---

def test_sends_to_the_exact_chat_and_verifies_each_step():
    # Log 2026-10-14 turn 13: the generic loop typed into nothing and reported "Pressed key 'enter'".
    app = FakeWhatsApp(["New Chapter 2", "New Chapter", "Anurodh"])

    outcome = _skill(app).send_message("New Chapter", "Praise the lord, it is a new begning")

    assert outcome.ok and outcome.stage == "sent"
    assert app.sent == [("New Chapter", "Praise the lord, it is a new begning")]
    assert outcome.evidence["chat_title"] == "New Chapter"
    assert outcome.evidence["chat_open_verified"] is True
    assert outcome.evidence["composer_text_verified"] is True
    assert outcome.evidence["composer_cleared_after_send"] is True


def test_ambiguous_chat_name_asks_instead_of_guessing():
    app = FakeWhatsApp(["Anurodh Work", "Anurodh Home"])

    outcome = _skill(app).send_message("Anurodh", "hello")

    assert not outcome.ok and outcome.stage == "search"
    assert set(outcome.candidates) == {"Anurodh Work", "Anurodh Home"}
    assert app.sent == [] and app.clicked == []


def test_unknown_chat_is_reported_and_nothing_is_typed():
    app = FakeWhatsApp(["Joel"])

    outcome = _skill(app).send_message("Anurodh", "hello")

    assert not outcome.ok and "couldn't find" in outcome.message
    assert app.sent == []


def test_logged_out_whatsapp_is_reported_as_such():
    app = FakeWhatsApp(["Joel"], logged_in=False)

    outcome = _skill(app).send_message("Joel", "hello")

    assert not outcome.ok and outcome.stage == "logged_in"
    assert "logged out" in outcome.message


def test_search_box_that_will_not_take_focus_stops_the_flow():
    app = FakeWhatsApp(["Joel"], focus_works=False)

    outcome = _skill(app).send_message("Joel", "hello")

    assert not outcome.ok and outcome.stage == "composed"
    assert app.sent == []


def test_unreadable_composer_never_sends():
    app = FakeWhatsApp(["Joel"], composer_readable=False)

    outcome = _skill(app).send_message("Joel", "hello")

    assert not outcome.ok and outcome.stage == "composed"
    assert app.sent == []


def test_existing_draft_is_left_alone():
    app = FakeWhatsApp(["Joel"], drafts={"Joel": "half-written thought"})

    outcome = _skill(app).send_message("Joel", "hello")

    assert not outcome.ok and "unsent text" in outcome.message
    assert app.sent == []
    assert app.values["composer"] == "half-written thought"


def test_whatsapp_is_launched_when_not_running():
    app = FakeWhatsApp(["Joel"], running=False)

    outcome = _skill(app).open_chat("Joel")

    assert outcome.ok and outcome.stage == "chat_opened"
    assert app.launched == ["whatsapp:"]
    assert app.sent == []


def test_capability_maps_outcomes_to_tool_results():
    app = FakeWhatsApp(["Joel"])
    capability = MessagingCapability(skill_factory=lambda: _skill(app))

    sent = capability.execute(ToolCall("c1", "whatsapp_send_message", {"chat": "Joel", "text": "hi"}))
    missing = capability.execute(ToolCall("c2", "whatsapp_send_message", {"chat": "Nobody", "text": "hi"}))

    assert sent.success and sent.verified and sent.verification_details["stage"] == "sent"
    assert not missing.success and str(missing.verification_status.value) == "failed"


# --- session integration ---

class _ScriptedProvider(LLMProvider):
    def __init__(self, script):
        self.script = list(script)
        self.offered = []

    @property
    def name(self):
        return "scripted"

    @property
    def model(self):
        return "scripted-model"

    def chat(self, messages, *, purpose="chat"):
        return LLMResponse(text="ok", provider="scripted", model="scripted-model")

    def chat_with_tools(self, messages, tools, *, tool_choice="auto", purpose="agent"):
        self.offered.append([t["function"]["name"] for t in tools])
        return self.script.pop(0)


def test_whatsapp_requests_route_to_the_skill_not_gui_primitives():
    names = ["open_application", "click", "type_text", "press_key", "read_screen",
             "whatsapp_send_message", "whatsapp_open_chat", "email_send"]
    capability = {n: ("messaging" if n.startswith("whatsapp") else "email" if n.startswith("email") else "desktop_control") for n in names}
    session = AgentSession.__new__(AgentSession)
    session.task = None
    session.tools = SimpleNamespace(model_tools=lambda: [ToolSpec(n, n, capability[n]) for n in names])

    offered = [t["function"]["name"] for t in session._select_model_tools(
        'open whatsapp, search chat called "New Chapter", in message box type "Praise the lord" and send this message'
    )]

    assert set(offered) == {"whatsapp_send_message", "whatsapp_open_chat"}


def test_send_asks_for_confirmation_then_runs_once_after_yes():
    app = FakeWhatsApp(["New Chapter"])
    registry = CapabilityRegistry()
    registry.register(Capability(
        name="messaging", description="m", supported_actions=MessagingCapability.SUPPORTED_ACTIONS,
        status=CapabilityStatus.AVAILABLE, handler=MessagingCapability(skill_factory=lambda: _skill(app)),
    ))
    from advi.core.execution_engine import ExecutionEngine
    from advi.core.verification import ToolVerifier

    tool_registry = ToolRegistry(registry)
    engine = ExecutionEngine(registry=registry, verifier=ToolVerifier())
    provider = _ScriptedProvider([
        LLMResponse(text="", provider="scripted", model="scripted-model", tool_calls=(
            LLMToolCall("c1", "whatsapp_send_message", {"chat": "New Chapter", "text": "Praise the lord"}),
        )),
        LLMResponse(text="Sent.", provider="scripted", model="scripted-model"),
    ])
    session = AgentSession(
        [provider], tool_registry=tool_registry,
        tool_runner=ToolRunner(engine, ToolGuard(tool_registry), tool_registry),
        tool_guard=ToolGuard(tool_registry),
    )

    ask = session.respond('send "Praise the lord" to New Chapter on whatsapp')
    assert "confirmation" in ask.text and "New Chapter" in ask.text and "Praise the lord" in ask.text
    assert app.sent == []

    session.respond("yes")
    assert app.sent == [("New Chapter", "Praise the lord")]


# --- regressions from the first live trial (2026-10-14) ---

def test_text_reaches_whatsapp_although_injected_keystrokes_do_not():
    # Live finding: SendInput keystrokes were dropped by WhatsApp's WebView2 host
    # (every trial failed at "composed"); posted characters arrive.
    app = FakeWhatsApp(["New Chapter"], groups={"New Chapter"}, keyboard_works=False)
    outcome = _skill(app).send_message("New Chapter", "test")
    assert outcome.ok, outcome.message
    assert app.sent == [("New Chapter", "test")]
    assert outcome.evidence["send_method"] == "send_button_invoke"


def test_search_uses_set_value_and_composer_never_does():
    app = FakeWhatsApp(["New Chapter"], groups={"New Chapter"}, post_works=False, keyboard_works=False)
    outcome = _skill(app).send_message("New Chapter", "test")
    assert outcome.evidence.get("chat_open_verified") is True  # search worked via SetValue
    assert not outcome.ok and outcome.stage == "composed"      # composer ignores SetValue
    assert app.sent == []


def test_a_dropped_post_is_retried_through_the_keyboard_route():
    app = FakeWhatsApp(["New Chapter"], groups={"New Chapter"}, drop_posts=1, keyboard_works=True)
    outcome = _skill(app).send_message("New Chapter", "hi")
    assert outcome.ok and app.sent == [("New Chapter", "hi")]


def test_multiline_dictation_is_sent_as_one_message():
    app = FakeWhatsApp(["Joel"])
    outcome = _skill(app).send_message("Joel", "line one\nline two")
    assert outcome.ok and app.sent == [("Joel", "line one line two")]


def test_failed_compose_removes_only_its_own_text():
    app = FakeWhatsApp(["Joel"])
    original_post = app.post_text

    def garbled(window, text):  # text lands but altered, so read-back fails
        return original_post(window, text + "?")

    app.post_text = garbled
    outcome = _skill(app).send_message("Joel", "hello")
    assert not outcome.ok and outcome.stage == "composed"
    assert app.sent == [] and app.values["composer"] == "\n"


def test_leftover_search_text_is_not_mistaken_for_logged_out():
    # Trial runs 3 and 5: the box still held "New Chapter", so its placeholder
    # name was gone and the skill reported "logged out".
    app = FakeWhatsApp(["New Chapter"], search_prefill="New Chapter", groups={"New Chapter"})
    outcome = _skill(app).send_message("New Chapter", "test")
    assert outcome.ok, outcome.message
    assert app.sent == [("New Chapter", "test")]


def test_group_title_is_read_without_its_date_stamp():
    # Trial run 2: "New Chapter 21/07/2025" was taken as the title.
    app = FakeWhatsApp(["New Chapter"], groups={"New Chapter"})
    outcome = _skill(app).open_chat("New Chapter")
    assert outcome.ok and outcome.evidence["chat_title"] == "New Chapter"


def test_chat_title_parsing():
    from advi.capabilities.apps.whatsapp import chat_title_from_label

    assert chat_title_from_label("New Chapter 21/07/2025 You :\xa0 Recipe") == "New Chapter"
    assert chat_title_from_label("Anurodh 9:41 pm hi") == "Anurodh"
    assert chat_title_from_label("Mom Yesterday call me") == "Mom"
    assert chat_title_from_label("Team 2024") == "Team 2024"
