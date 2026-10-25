"""Is the PC playing sound (music, a video) that isn't ADVI?

While other audio plays, always-on listening only accepts sentences that
include ADVI's name, so song lyrics or a video never become commands. The
level comes from Windows' master output peak meter (IAudioMeterInformation)
through comtypes, which pywinauto already installs.
"""

from __future__ import annotations

from collections import deque
import ctypes
import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)


def _meter():
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    from ctypes import POINTER, c_float, c_uint, c_void_p
    from ctypes.wintypes import DWORD

    class IAudioMeterInformation(IUnknown):
        _iid_ = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
        _methods_ = [COMMETHOD([], HRESULT, "GetPeakValue", (["out"], POINTER(c_float), "pfPeak"))]

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "dwClsCtx"),
                      (["in"], c_void_p, "pActivationParams"), (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
        ]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_uint, "dataFlow"), (["in"], DWORD, "mask"),
                      (["out"], POINTER(c_void_p), "devices")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_uint, "dataFlow"), (["in"], c_uint, "role"),
                      (["out"], POINTER(POINTER(IMMDevice)), "device")),
        ]

    comtypes.CoInitialize()
    enumerator = comtypes.CoCreateInstance(
        GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"), IMMDeviceEnumerator, comtypes.CLSCTX_ALL)
    device = enumerator.GetDefaultAudioEndpoint(0, 1)  # eRender, eMultimedia
    unknown = device.Activate(ctypes.byref(IAudioMeterInformation._iid_), comtypes.CLSCTX_ALL, None)
    return unknown.QueryInterface(IAudioMeterInformation)


class SystemAudioMonitor:
    """Samples the output level ten times a second, skipping moments ADVI itself makes sound."""

    def __init__(
        self,
        *,
        own_sound: Callable[[], bool] = lambda: False,
        read_peak: Callable[[], float] | None = None,
        clock: Callable[[], float] = time.monotonic,
        threshold: float = 0.02,
    ) -> None:
        self.own_sound = own_sound
        self._read_peak = read_peak
        self.clock = clock
        self.threshold = threshold
        self._samples: deque[tuple[float, float]] = deque(maxlen=60)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if self._read_peak is None:
            ready = threading.Event()
            failed: list[str] = []

            def run() -> None:
                try:
                    meter = _meter()  # COM objects stay on the thread that made them
                    self._read_peak = lambda: float(meter.GetPeakValue())
                except Exception as exc:
                    failed.append(str(exc))
                    ready.set()
                    return
                ready.set()
                self._loop()

            self._thread = threading.Thread(target=run, name="advi-system-audio", daemon=True)
            self._thread.start()
            ready.wait(3.0)
            if failed:
                log.info("System audio level unavailable (%s); the name check during music is off.", failed[0])
                return False
            return True
        self._thread = threading.Thread(target=self._loop, name="advi-system-audio", daemon=True)
        self._thread.start()
        return True

    def _loop(self) -> None:
        while not self._stop.wait(0.1):
            self.sample()

    def sample(self) -> None:
        if self._read_peak is None or self.own_sound():
            return
        try:
            self._samples.append((self.clock(), self._read_peak()))
        except Exception:
            log.debug("Peak meter read failed", exc_info=True)

    def other_audio_playing(self, window: float = 2.0) -> bool:
        """True when, over the last couple of seconds, sound was playing most of the time."""
        now = self.clock()
        recent = [peak for at, peak in self._samples if now - at <= window]
        if len(recent) < 3:
            return False
        loud = sum(1 for peak in recent if peak >= self.threshold)
        return loud / len(recent) >= 0.5

    def close(self) -> None:
        self._stop.set()
