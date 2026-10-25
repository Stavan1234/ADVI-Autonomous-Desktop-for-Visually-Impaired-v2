"""Voice start-up: speech recognition, microphone, hotkey and always-on listening.

Phase 11 (2026-10-24) adapted the voice_input prototype as push-to-talk. The
first live session showed that pressing a key for every sentence is wrong for
a blind user, so Phase 12 listens continuously (see conversation.py).

Since 2026-10-25, ADVI's speech and sounds and the microphone share one duplex
stream with echo cancellation (audio_io.py), "sleep" closes ADVI, and the
separate wake listener (advi/wake) starts it again. The hotkey interrupts
ADVI's speech while it runs, and starts ADVI (through the wake listener) when
it is closed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Sequence

from advi.voice.audio_io import DuplexAudio
from advi.voice.conversation import VoiceConversation
from advi.voice.earcons import Earcons
from advi.voice.hotkey import DEFAULT_HOTKEY, GlobalHotkey, HotkeyError
from advi.voice.recorder import MicrophoneUnavailable, open_microphone, preroll_frames
from advi.voice.system_audio import SystemAudioMonitor
from advi.voice.transcriber import (
    DEFAULT_CLOUD_STT_MODEL,
    DEFAULT_STT_MODEL,
    CloudTranscriber,
    DualTranscriber,
    LocalTranscriber,
)

log = logging.getLogger(__name__)

_OFF = {"0", "off", "false", "no"}
# Tried in order; Windows refuses a key another program already owns. On the dev
# laptop Ctrl+Alt+Space was taken (Flow Launcher / PowerToys are running).
# Ctrl+Alt+V was left out: it is Paste Special in Word and Excel.
HOTKEY_CHOICES = (DEFAULT_HOTKEY, "ctrl+alt+z", "ctrl+alt+enter")


def voice_enabled() -> bool:
    return os.getenv("ADVI_VOICE", "on").strip().casefold() not in _OFF


def transcriber_from_env(
    asset_root: Path,
    groq_api_keys: Sequence[str] = (),
    *,
    notify: Callable[[str], Any] = lambda text: None,
) -> DualTranscriber:
    """Groq's Whisper Turbo when online (ADVI_STT_CLOUD=off keeps audio on this PC), local otherwise."""
    language = os.getenv("ADVI_VOICE_LANGUAGE", "en").strip() or None
    local = LocalTranscriber(
        os.getenv("ADVI_STT_MODEL", DEFAULT_STT_MODEL).strip() or DEFAULT_STT_MODEL,
        download_root=asset_root / "whisper",
        language=language,
    )
    cloud = None
    if groq_api_keys and os.getenv("ADVI_STT_CLOUD", "on").strip().casefold() not in _OFF:
        cloud = CloudTranscriber(
            groq_api_keys,
            model=os.getenv("ADVI_STT_CLOUD_MODEL", DEFAULT_CLOUD_STT_MODEL).strip() or DEFAULT_CLOUD_STT_MODEL,
            language=language,
        )
    return DualTranscriber(local, cloud, notify=notify)


def open_duplex_audio() -> DuplexAudio | None:
    """ADVI's shared speaker + echo-cancelled microphone stream; None if it can't open."""
    if os.getenv("ADVI_ECHO_CANCEL", "on").strip().casefold() in _OFF:
        return None
    try:
        return DuplexAudio(preroll_frames=preroll_frames())
    except MicrophoneUnavailable as exc:
        log.warning("Duplex audio unavailable (%s); using the microphone without echo cancellation.", exc)
        return None


def spoken_hotkey(hotkey: str) -> str:
    names = {"ctrl": "Control", "alt": "Alt", "shift": "Shift", "win": "Windows"}
    return " ".join(names.get(p.strip().casefold(), p.strip().capitalize()) for p in hotkey.split("+"))


def start_voice(
    transcriber: DualTranscriber,
    *,
    submit: Callable[[str], Any],
    is_busy: Callable[[], bool],
    say: Callable[[str], Any],
    is_quiet: Callable[[], bool],
    stop_speech: Callable[[], Any],
    request_stop: Callable[[], Any] = lambda: None,
    request_exit: Callable[[], Any] = lambda: None,
    spoken_recently: Callable[[], str] = lambda: "",
    pause_speech: Callable[[], Any] | None = None,
    resume_speech: Callable[[], Any] = lambda: None,
    audio: DuplexAudio | None = None,
    wake_word: bool = False,
) -> tuple[VoiceConversation | None, str]:
    """Open the microphone, start listening, register the hotkey; returns (voice, sentence for the user).

    ``audio`` is the shared duplex stream (speech already plays through it); without
    it the raw microphone is used. ``wake_word``: the wake listener is running, so
    "sleep" can promise that "wake up ADVI" brings ADVI back.
    """
    if not transcriber.ready:
        return None, "Voice input is off because no speech recognition could be loaded."
    configured = os.getenv("ADVI_VOICE_HOTKEY", "").strip()
    candidates = (configured,) if configured else HOTKEY_CHOICES
    try:
        recorder = open_microphone(source=audio.capture_source() if audio is not None else None)
    except MicrophoneUnavailable as exc:
        log.warning("Microphone unavailable: %s", exc)
        return None, "Voice input is off because I can't reach a microphone."
    earcons = Earcons(player=audio.play_earcon) if audio is not None else Earcons()
    echo_cancelled = bool(audio is not None and audio.echo_cancelling)
    voice = VoiceConversation(
        recorder=recorder, transcriber=transcriber, submit=submit, is_busy=is_busy, say=say,
        is_quiet=is_quiet, stop_speech=stop_speech, request_stop=request_stop, request_exit=request_exit,
        spoken_recently=spoken_recently, earcons=earcons, echo_cancelled=echo_cancelled,
        pause_speech=pause_speech, resume_speech=resume_speech,
        wake_hint="Say wake up ADVI when you need me." if wake_word else "Start ADVI again when you need me.",
    )
    chosen = None
    for hotkey_text in candidates:
        try:
            hotkey = GlobalHotkey(hotkey_text, voice.on_hotkey)
        except HotkeyError as exc:
            voice.close()
            return None, f"Voice input is off because the hotkey setting is not valid: {exc}."
        if hotkey.start():
            voice.hotkey, chosen = hotkey, hotkey_text
            break
        log.info("Voice hotkey %s is %s; trying the next one.", hotkey_text, hotkey.error)
    # ADVI's own sound is skipped, so only music or a video counts as "other audio".
    monitor = SystemAudioMonitor(own_sound=lambda: not is_quiet() or earcons.playing)
    if monitor.start():
        voice.monitor = monitor
        voice.other_audio = monitor.other_audio_playing
    if chosen:
        key_note = f" {spoken_hotkey(chosen)} stops me talking."
    else:
        tried = " or ".join(spoken_hotkey(k) for k in candidates)
        key_note = f" There's no stop key because {tried} is already used by another program."
    voice.start()
    log.info("Voice started: echo cancellation %s, wake word %s.", "on" if echo_cancelled else "off",
             "on" if wake_word else "off")
    wake_note = " Say wake up ADVI to bring me back." if wake_word else ""
    return voice, (
        "I'm listening; just talk to me. When I finish speaking you'll hear a soft rising chime: that's your turn. "
        "You can talk over me to interrupt. Say sleep or exit to close me, "
        f"or ask what do the sounds mean.{wake_note}{key_note}"
    )
