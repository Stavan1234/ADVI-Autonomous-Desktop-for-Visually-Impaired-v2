"""Deterministic commands that run without a language-model request.

The 2026-10-14 log ended with every AI key out of daily quota: "send an email
to stavan using david fallback saying good evening" and similar clear commands
could not even start. Voice assistants keep a small set of unambiguous commands
on-device. This module recognises them conservatively; anything it does not
match exactly goes to the model as before. Messages and emails still require
the user's spoken confirmation before sending.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

_POLITE = re.compile(
    r"^(?:(?:hey\s+|ok(?:ay)?\s+)?advi[,\s]+)?(?:please\s+|can you\s+|could you\s+|would you\s+|will you\s+|i want you to\s+|kindly\s+)*",
    re.I,
)
_TRAILING = re.compile(r"[\s.!?]+$|\s+(?:please|for me|now)[\s.!?]*$", re.I)

SITES: dict[str, str] = {
    "youtube": "https://www.youtube.com/",
    "youtube music": "https://music.youtube.com/",
    "gmail": "https://mail.google.com/mail/",
    "google": "https://www.google.com/",
    "reddit": "https://www.reddit.com/",
    "amazon": "https://www.amazon.in/",
    "flipkart": "https://www.flipkart.com/",
    "wikipedia": "https://www.wikipedia.org/",
    "github": "https://github.com/",
    "linkedin": "https://www.linkedin.com/",
    "netflix": "https://www.netflix.com/",
    "instagram": "https://www.instagram.com/",
    "facebook": "https://www.facebook.com/",
    "twitter": "https://x.com/",
    "x": "https://x.com/",
    "chatgpt": "https://chatgpt.com/",
    "google drive": "https://drive.google.com/",
    "drive": "https://drive.google.com/",
    "google maps": "https://www.google.com/maps",
    "maps": "https://www.google.com/maps",
    "whatsapp web": "https://web.whatsapp.com/",
}

SEARCH_URLS: dict[str, str] = {
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "youtube music": "https://music.youtube.com/search?q={q}",
    "google": "https://www.google.com/search?q={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    "amazon": "https://www.amazon.in/s?k={q}",
    "flipkart": "https://www.flipkart.com/search?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "github": "https://github.com/search?q={q}",
    "gmail": "https://mail.google.com/mail/u/0/#search/{q}",
    "google maps": "https://www.google.com/maps/search/{q}",
    "maps": "https://www.google.com/maps/search/{q}",
}

APPS: dict[str, str] = {
    "notepad": "Notepad", "calculator": "Calculator", "calc": "Calculator",
    "word": "Microsoft Word", "microsoft word": "Microsoft Word", "ms word": "Microsoft Word",
    "excel": "Microsoft Excel", "microsoft excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint", "microsoft powerpoint": "Microsoft PowerPoint", "ppt": "Microsoft PowerPoint",
    "outlook": "Outlook", "teams": "Microsoft Teams", "microsoft teams": "Microsoft Teams",
    "whatsapp": "WhatsApp", "telegram": "Telegram", "spotify": "Spotify", "vlc": "VLC media player",
    "file explorer": "File Explorer", "explorer": "File Explorer", "settings": "Settings",
    "paint": "Paint", "camera": "Camera", "photos": "Photos", "clock": "Clock",
    "task manager": "Task Manager", "command prompt": "Command Prompt", "terminal": "Terminal",
    "vs code": "Visual Studio Code", "visual studio code": "Visual Studio Code", "zoom": "Zoom",
    "edge": "Microsoft Edge", "microsoft edge": "Microsoft Edge",
}
_BROWSER_NAMES = {"chrome", "google chrome", "browser", "the browser", "web browser"}

FOLDERS: dict[str, str] = {
    "downloads": "Downloads", "download": "Downloads", "desktop": "Desktop", "documents": "Documents",
    "document": "Documents", "videos": "Videos", "video": "Videos", "music": "Music",
    "pictures": "Pictures", "photos": "Pictures",
}


@dataclass(frozen=True)
class LocalIntent:
    kind: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # A follow-up the session resolves (e.g. contact_lookup before an email).
    detail: dict[str, Any] = field(default_factory=dict)


def _clean(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = _POLITE.sub("", value)
    return _TRAILING.sub("", value).strip()


def _unquote(value: str) -> str:
    return value.strip().strip("\"'“”‘’").strip()


def _folder(word: str) -> str | None:
    return FOLDERS.get(re.sub(r"\s+folder$", "", word.strip().casefold()).removeprefix("my ").removeprefix("the ").strip())


def match_local_intent(text: str) -> LocalIntent | None:
    raw = _clean(text)
    if not raw or len(raw) > 400:
        return None
    lower = raw.casefold()
    for matcher in (_email, _whatsapp, _media_control, _play, _site_search, _open_site, _files, _open_app):
        intent = matcher(raw, lower)
        if intent is not None:
            return intent
    return None


# --- communication -----------------------------------------------------------

def _email(raw: str, lower: str) -> LocalIntent | None:
    david = bool(re.search(r"\b(?:using|via|with|through)\s+(?:the\s+)?david(?:\s+fallback)?\b|\bdavid\s+fallback\b", lower))
    text = re.sub(r"\s*\b(?:using|via|with|through)\s+(?:the\s+)?david(?:\s+fallback)?\b|\bdavid\s+fallback\b\s*", " ", raw, flags=re.I).strip()
    text = re.sub(r"\s*\b(?:using|via|through|on|in)\s+gmail\b", "", text, flags=re.I).strip()
    match = re.match(
        r"^(?:send|write|compose|drop)\s+(?:an?\s+)?(?:e-?mail|mail|message)\s+to\s+(?P<to>[^,]+?)\s*"
        r"(?:,\s*)?(?:(?:with\s+(?:the\s+)?)?subject\s+(?P<subject>\"[^\"]+\"|“[^”]+”|.+?)\s+(?:and\s+)?)?"
        r"(?:saying|that says|which says|with (?:the )?(?:message|body)|message|body|telling (?:him|her|them)|:)\s*(?P<body>.+)$",
        text, re.I,
    ) or re.match(
        r"^e-?mail\s+(?P<to>[^,]+?)\s+(?:saying|that says|:)\s*(?P<body>.+)$", text, re.I,
    )
    if not match:
        return None
    if "mail" not in lower and "email" not in lower and not david:
        return None  # "send a message to X saying Y" without email words is ambiguous
    to = _unquote(match.group("to"))
    body = _unquote(match.group("body"))
    subject = _unquote(match.groupdict().get("subject") or "")
    if not to or not body or re.search(r"\b(?:whatsapp|telegram)\b", lower):
        return None
    return LocalIntent("email", "david_fallback" if david else "email_draft_create",
                       {"to": to, "subject": subject, "body": body}, {"via_david": david})


def _whatsapp(raw: str, lower: str) -> LocalIntent | None:
    if "telegram" in lower and "whatsapp" not in lower:
        swapped = re.sub(r"telegram", "whatsapp", raw, flags=re.I)
        found = _whatsapp(swapped, swapped.casefold())
        if found is None:
            return None
        return LocalIntent("telegram", "telegram_send_message", found.arguments, {"app": "telegram"})
    if "whatsapp" not in lower:
        return None
    quoted_chat = re.search(r"\b(?:chat|group|contact)\s+(?:called|named)?\s*[\"“]([^\"”]+)[\"”]", raw, re.I)
    quoted_text = re.search(r"\b(?:message|saying|text|type)\s*(?:box\s+type\s*)?[\"“]([^\"”]+)[\"”]", raw, re.I)
    if quoted_chat and quoted_text:
        return LocalIntent("whatsapp", "whatsapp_send_message", {"chat": quoted_chat.group(1).strip(), "text": quoted_text.group(1).strip()})
    patterns = (
        r"^(?:open whatsapp(?:\s+web)?\s*(?:,|and)\s*)?send\s+(?:a\s+)?(?:whatsapp\s+)?(?:message|msg|text)?\s*to\s+(?P<chat>.+?)\s+(?:on|via|in|over)\s+whatsapp(?:\s+web)?\s*(?:saying|that says|:)\s*(?P<text>.+)$",
        r"^(?:open whatsapp(?:\s+web)?\s*(?:,|and)\s*)?send\s+(?:a\s+)?whatsapp\s+(?:message|msg|text)?\s*to\s+(?P<chat>.+?)\s*(?:saying|that says|:)\s*(?P<text>.+)$",
        r"^whatsapp\s+(?P<chat>.+?)\s*(?:saying|that says|:)\s*(?P<text>.+)$",
        r"^(?:message|text|tell)\s+(?P<chat>.+?)\s+(?:on|via|in|over)\s+whatsapp\s*(?:saying|that says|that|:)\s*(?P<text>.+)$",
        r"^(?:open whatsapp(?:\s+web)?\s*(?:,|and)\s*)?send\s+(?:a\s+)?(?:message\s+)?(?P<text>[\"“][^\"”]+[\"”]|\S+(?:\s+\S+){0,6}?)\s+to\s+(?P<chat>.+?)(?:\s+(?:on|via|in)\s+whatsapp(?:\s+web)?)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, raw, re.I)
        if match:
            chat, message = _unquote(match.group("chat")), _unquote(match.group("text"))
            chat = re.sub(r"\s+(?:on|via|in)\s+whatsapp(?:\s+web)?$", "", chat, flags=re.I).strip()
            if chat and message and "whatsapp" not in chat.casefold():
                return LocalIntent("whatsapp", "whatsapp_send_message", {"chat": chat, "text": message})
    return None


# --- media ---------------------------------------------------------------------

def _media_control(raw: str, lower: str) -> LocalIntent | None:
    table = {
        r"(?:pause|stop)(?: the| this)? (?:music|song|video|playback|player)|pause(?: it| that)?": "pause",
        r"(?:resume|unpause|continue)(?: the| this)?(?: music| song| video| playback)?|play it again": "resume",
        r"(?:next|skip)(?: the| this)? (?:song|track|video)|next": "next",
        r"previous (?:song|track|video)|go back a (?:song|track)": "previous",
        r"(?:turn (?:the )?volume up|turn it up|volume up|increase (?:the )?volume|louder|raise (?:the )?volume)": "volume_up",
        r"(?:turn (?:the )?volume down|turn it down|volume down|decrease (?:the )?volume|lower (?:the )?volume|quieter)": "volume_down",
        r"(?:mute|unmute)(?: (?:the )?(?:sound|volume|audio|computer|laptop))?": "mute",
    }
    for pattern, action in table.items():
        if re.fullmatch(pattern, lower):
            return LocalIntent("media", "media_control", {"action": action})
    return None


def _play(raw: str, lower: str) -> LocalIntent | None:
    match = re.match(r"^play\s+(?P<q>.+?)\s+(?:on|in|from|using)\s+(?P<svc>youtube music|yt music|youtube)$", raw, re.I)
    if match:
        query = _unquote(match.group("q"))  # "praise song" is the search, not "praise"
        service = "youtube_music" if "music" in match.group("svc").casefold() else "youtube"
        return LocalIntent("play", "play_media", {"query": query, "service": service}) if query else None
    match = re.match(
        r"^play\s+(?P<q>.+?)\s+(?:video|song|audio|file)?\s*(?:from|in|on)\s+(?:my\s+)?(?:(?P<folder>videos?|music|downloads|desktop)\s+folder|(?P<folder2>videos?|music|downloads|desktop)|laptop|computer|pc)$",
        raw, re.I,
    )
    if match:
        query = _unquote(re.sub(r"\s+(?:song|video|audio)$", "", match.group("q"), flags=re.I))
        folder = match.group("folder") or match.group("folder2")
        arguments = {"query": query}
        if folder:
            arguments["folder"] = FOLDERS.get(folder.casefold(), folder.title())
        return LocalIntent("play", "play_local_media", arguments) if query else None
    match = re.match(r"^(?:open|go to)\s+(?P<svc>youtube music|yt music|youtube)\s+(?:and\s+)?play\s+(?P<q>.+)$", raw, re.I)
    if match:
        service = "youtube_music" if "music" in match.group("svc").casefold() else "youtube"
        return LocalIntent("play", "play_media", {"query": _unquote(match.group("q")), "service": service})
    match = re.match(r"^play\s+(?:the\s+song\s+|the\s+video\s+)?(?P<q>.+)$", raw, re.I)
    if match:
        query = _unquote(match.group("q"))
        vague = re.match(r"^(?:something|anything|some\b|a song|a video|music|songs?|videos?|it|that|this|again|next|previous|my\b|the\b)", query, re.I)
        elsewhere = re.search(r"\b(?:spotify|netflix|prime|hotstar|game|file|folder|laptop|computer|pc|playlist|downloads|desktop|vlc)\b", query, re.I)
        if query and not vague and not elsewhere:
            # A bare "play <title>" means YouTube, as with most voice assistants.
            return LocalIntent("play", "play_media", {"query": query, "service": "youtube"}, {"bare": True})
    return None


# --- browser -------------------------------------------------------------------

def _site_key(name: str) -> str | None:
    key = name.strip().casefold().removeprefix("the ").strip()
    return key if key in SEARCH_URLS or key in SITES else None


def _site_search(raw: str, lower: str) -> LocalIntent | None:
    match = re.match(r"^(?:search|look up|find)\s+(?:for\s+)?(?P<q>.+?)\s+(?:on|in|at)\s+(?P<site>[a-z ]+?)$", raw, re.I) or re.match(
        r"^(?:search|go to|open)\s+(?P<site>[a-z ]+?)\s+(?:and\s+)?(?:search|look)\s+(?:for\s+)?(?P<q>.+)$", raw, re.I)
    if not match:
        general = re.match(r"^(?:search(?: the web| online| google)?(?: for)?|google|look up)\s+(?P<q>.+)$", raw, re.I)
        if general and not re.search(r"\b(?:file|folder|pdf|docx?|my|photo|picture|video file|downloads|desktop|documents|email|mail|inbox)s?\b", general.group("q"), re.I):
            query = _unquote(general.group("q"))
            url = SEARCH_URLS["google"].format(q=urllib.parse.quote_plus(query))
            return LocalIntent("site_search", "navigate", {"url": url}, {"site": "google", "query": query})
        return None
    site = _site_key(match.group("site"))
    query = _unquote(match.group("q"))
    if not site or site not in SEARCH_URLS or not query:
        return None
    url = SEARCH_URLS[site].format(q=urllib.parse.quote_plus(query))
    return LocalIntent("site_search", "navigate", {"url": url}, {"site": site, "query": query})


def _open_site(raw: str, lower: str) -> LocalIntent | None:
    match = re.match(r"^(?:open|go to|visit|launch|take me to)\s+(?P<site>.+?)(?:\s+(?:website|site|in (?:the )?browser|in chrome))?$", raw, re.I)
    if not match:
        return None
    name = match.group("site").strip()
    key = _site_key(name)
    if key and key in SITES:
        return LocalIntent("open_site", "navigate", {"url": SITES[key]}, {"site": key})
    if re.fullmatch(r"(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?", name, re.I):
        url = name if name.casefold().startswith("http") else f"https://{name}"
        return LocalIntent("open_site", "navigate", {"url": url}, {"site": name})
    return None


# --- files -----------------------------------------------------------------------

def _files(raw: str, lower: str) -> LocalIntent | None:
    if re.fullmatch(r"undo(?: that| it| the last (?:change|move|organi[sz]ation))?|put (?:them|it|the files) back", lower):
        return LocalIntent("files", "undo_file_operation", {})
    match = re.fullmatch(r"(?:organi[sz]e|sort|tidy(?: up)?|clean up|arrange)\s+(?:my\s+|the\s+)?(?P<folder>\w+)(?:\s+folder)?(?:\s+(?:files\s+)?by\s+(?:file\s+)?types?)?", lower)
    if match and _folder(match.group("folder")):
        return LocalIntent("files", "organize_folder", {"folder": _folder(match.group("folder"))})
    match = re.fullmatch(
        r"move\s+(?:all\s+)?(?:the\s+|my\s+)?(?P<ext>[a-z0-9]{2,5})s?\s+(?:files?\s+)?(?:from\s+(?:my\s+|the\s+)?(?P<src>\w+)(?:\s+folder)?\s+)?(?:in)?to\s+(?:a\s+|the\s+|my\s+)?(?:folder\s+(?:called|named)\s+)?[\"“]?(?P<dst>[\w ./\\-]+?)[\"”]?(?:\s+folder)?",
        raw, re.I,  # raw keeps the user's casing for the new folder name ("PDF")
    )
    if match:
        source = _folder(match.group("src") or "downloads")
        dest_word = match.group("dst").strip()
        destination = _folder(dest_word) or (f"{source}/{dest_word}" if source and not re.search(r"[\\/:]", dest_word) else dest_word)
        if source:
            return LocalIntent("files", "move_files", {"source": source, "destination": destination, "extensions": [match.group("ext").casefold()]})
    match = re.fullmatch(r"(?:open|show)\s+(?:my\s+|the\s+)?(?P<folder>\w+)\s+folder", lower) or re.fullmatch(
        r"(?:open|show)\s+(?:my\s+|the\s+)?(?P<folder>downloads|desktop|documents|pictures|videos)", lower)  # not "photos"/"music": apps
    if match and _folder(match.group("folder")):
        return LocalIntent("files", "open_folder", {"path": _folder(match.group("folder"))})
    match = re.fullmatch(
        r"(?:create|make)\s+(?:a\s+)?(?:new\s+)?folder\s+(?:called|named)\s+[\"“]?(?P<name>[\w .-]+?)[\"”]?\s+(?:on|in)\s+(?:my\s+|the\s+)?(?P<where>\w+)(?:\s+folder)?",
        raw, re.I,
    )
    if match and _folder(match.group("where")) and match.group("name").strip():
        return LocalIntent("files", "create_folder", {"path": f"{_folder(match.group('where'))}/{match.group('name').strip()}"})
    return None


# --- applications ----------------------------------------------------------------

def _open_app(raw: str, lower: str) -> LocalIntent | None:
    # "open notepad and type hello from advi": literal text, no AI needed.
    # "write" only with quoted text; "write a poem about rain" needs the AI.
    typing = re.match(r"^(?:open|launch|start)\s+(?:the\s+)?notepad\s*(?:,|and|then)?\s*(?:and\s+)?(?:type|enter)\s+(?P<text>.+)$", raw, re.I) or re.match(
        r"^(?:open|launch|start)\s+(?:the\s+)?notepad\s*(?:,|and|then)?\s*(?:and\s+)?write\s+(?P<text>[\"“].+[\"”])$", raw, re.I)
    if typing:
        text = _unquote(typing.group("text"))
        if text:
            return LocalIntent("app_type", "open_application", {"application": "Notepad"}, {"type_text": text})
    closing = re.fullmatch(r"(?:close|quit|exit)\s+(?:the\s+|my\s+)?(?P<app>[a-z .]+?)(?:\s+(?:app|application|window))?", lower)
    if closing:
        name = closing.group("app").strip()
        app = "Google Chrome" if name in _BROWSER_NAMES else APPS.get(name)
        return LocalIntent("close_app", "close_application", {"application": app}) if app else None
    match = re.fullmatch(r"(?:open|launch|start|run)\s+(?:the\s+|my\s+)?(?P<app>[a-z .]+?)(?:\s+(?:app|application))?", lower)
    if not match:
        return None
    name = match.group("app").strip()
    if name in _BROWSER_NAMES:
        return LocalIntent("open_app", "open_browser", {})
    app = APPS.get(name)
    return LocalIntent("open_app", "open_application", {"application": app}) if app else None
