"""Short spoken progress phrases for tool calls.

ADVI is used by voice, often without looking at the screen. Multi-step tasks
ran 20-60 s in silence (runtime log, 2026-10-14). Each meaningful operation
gets one plain phrase as it starts; observations, clicks and keystrokes stay
silent because narrating every step is noise for a screen-reader user.
"""
from __future__ import annotations

import re
from pathlib import PureWindowsPath
from typing import Any, Callable
from urllib.parse import urlparse

_MAX_ARG_CHARS = 60


def _arg(arguments: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            text = re.sub(r"\s+", " ", value.strip())
            return text if len(text) <= _MAX_ARG_CHARS else text[: _MAX_ARG_CHARS - 1] + "…"
    return ""


def _file(arguments: dict[str, Any], *keys: str) -> str:
    value = _arg(arguments, *keys)
    return PureWindowsPath(value).name if value else ""


def _site(arguments: dict[str, Any]) -> str:
    url = _arg(arguments, "url")
    if not url:
        return ""
    host = urlparse(url if "://" in url else f"https://{url}").netloc
    return host.removeprefix("www.") or url


def _with(template: str, value: str, fallback: str) -> str:
    return template.format(value) if value else fallback


_PHRASES: dict[str, Callable[[dict[str, Any]], str]] = {
    "open_application": lambda a: _with("Opening {}.", _arg(a, "application"), "Opening the app."),
    "close_application": lambda a: _with("Closing {}.", _arg(a, "application"), "Closing the app."),
    "whatsapp_send_message": lambda a: _with("Sending your WhatsApp message to {}.", _arg(a, "chat"), "Sending your WhatsApp message."),
    "whatsapp_open_chat": lambda a: _with("Opening {} on WhatsApp.", _arg(a, "chat"), "Opening WhatsApp."),
    "telegram_send_message": lambda a: _with("Sending your Telegram message to {}.", _arg(a, "chat"), "Sending your Telegram message."),
    "telegram_open_chat": lambda a: _with("Opening {} on Telegram.", _arg(a, "chat"), "Opening Telegram."),
    "play_local_media": lambda a: _with("Playing {} from your computer.", _arg(a, "query", "name"), "Playing your file."),
    "play_media": lambda a: _with("Looking up {}.", _arg(a, "query"), "Finding that for you."),
    "navigate": lambda a: _with("Opening {}.", _site(a), "Opening the page."),
    "open_browser": lambda a: "Opening the browser.",
    "search": lambda a: _with("Searching for {}.", _arg(a, "query"), "Searching."),
    "research_web": lambda a: _with("Researching {}.", _arg(a, "query"), "Researching that."),
    "word_create_document": lambda a: _with("Writing {} in Word.", _file(a, "path", "filename"), "Writing your document in Word."),
    "create_file": lambda a: _with("Creating {}.", _file(a, "path", "filename"), "Creating the file."),
    "save_file": lambda a: _with("Saving {}.", _file(a, "path", "filename"), "Saving the file."),
    "save_document_as": lambda a: _with("Saving as {}.", _file(a, "path", "filename"), "Saving the document."),
    "read_file": lambda a: _with("Reading {}.", _file(a, "path"), "Reading the file."),
    "open_file": lambda a: _with("Opening {}.", _file(a, "path"), "Opening the file."),
    "open_folder": lambda a: _with("Opening the {} folder.", _file(a, "path"), "Opening the folder."),
    "find_file": lambda a: _with("Looking for {}.", _arg(a, "name"), "Looking for the file."),
    "move_files": lambda a: _with("Moving files to {}.", _file(a, "destination", "to"), "Moving the files."),
    "organize_folder": lambda a: _with("Organizing {}.", _file(a, "folder", "path"), "Organizing the folder."),
    "undo_file_operation": lambda a: "Undoing the last file change.",
    "create_folder": lambda a: _with("Creating the {} folder.", _file(a, "path"), "Creating the folder."),
    "email_draft_create": lambda a: _with("Drafting the email to {}.", _arg(a, "to"), "Drafting the email."),
    "email_send": lambda a: "Sending the email.",
    "email_read": lambda a: "Checking your inbox.",
    "contact_lookup": lambda a: _with("Looking up {}.", _arg(a, "name", "query"), "Looking up the contact."),
    "david_fallback": lambda a: "Trying another way.",
}


def progress_phrase(tool_name: str, arguments: dict[str, Any] | None) -> str | None:
    """The phrase to speak when ``tool_name`` starts, or None to stay quiet."""
    builder = _PHRASES.get(str(tool_name or ""))
    if builder is None:
        return None
    try:
        return builder(dict(arguments or {}))
    except Exception:
        return None
