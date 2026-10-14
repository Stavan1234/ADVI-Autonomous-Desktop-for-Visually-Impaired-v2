from __future__ import annotations

import os
import platform
import socket
import time
from pathlib import Path
from typing import Any

from advi.tools.contracts import ToolCall, ToolResult, VerificationStatus


class SystemCapability:
    """Small, deterministic read-only capability for local machine information."""

    SUPPORTED_ACTIONS = {"get_system_info", "get_system_status", "get_battery_status"}

    def _execute_get_system_info(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        return self.execute(call, approval_granted=approval_granted)

    def _execute_get_system_status(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        return self.execute(call, approval_granted=approval_granted)

    def _execute_get_battery_status(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        return self.execute(call, approval_granted=approval_granted)

    def execute(self, call: ToolCall, *, approval_granted: bool = False) -> ToolResult:
        action = call.action
        try:
            if action == "get_battery_status":
                data = self._battery()
                readable = self._battery_text(data)
            elif action == "get_system_info":
                data = self._system_info()
                readable = self._info_text(data)
            elif action == "get_system_status":
                data = self._system_status()
                readable = self._status_text(data)
            else:
                return ToolResult(call_id=call.call_id, name=action, success=False, error=f"Unsupported system action: {action}")
        except Exception as exc:
            return ToolResult(call_id=call.call_id, name=action, success=False, error=f"Could not read system information: {exc}")

        return ToolResult(
            call_id=call.call_id,
            name=action,
            success=True,
            output=data,
            human_readable=readable,
            verified=True,
            verification_status=VerificationStatus.VERIFIED,
            verification_details={"method": "local_system_probe"},
            metadata={"read_only": True},
        )

    @staticmethod
    def _psutil():
        import psutil
        return psutil

    @classmethod
    def _battery(cls) -> dict[str, Any]:
        psutil = cls._psutil()
        try:
            battery = psutil.sensors_battery()
        except (OSError, RuntimeError, NotImplementedError):
            return {"available": False, "reason": "battery_not_available"}
        if battery is None:
            return {"available": False, "reason": "battery_not_available"}
        seconds = battery.secsleft
        return {
            "available": True,
            "percent": round(float(battery.percent), 1),
            "plugged_in": bool(battery.power_plugged),
            "charging": bool(battery.power_plugged and (seconds in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) or battery.percent < 100)),
            "seconds_left": None if seconds in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) else int(seconds),
        }

    @classmethod
    def _system_info(cls) -> dict[str, Any]:
        psutil = cls._psutil()
        root = Path.home().anchor or os.environ.get("SystemDrive", "C:\\")
        disk = psutil.disk_usage(root)
        memory = psutil.virtual_memory()
        uname = platform.uname()
        return {
            "hostname": socket.gethostname(),
            "os": platform.system(),
            "os_release": platform.release(),
            "os_version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor() or uname.processor,
            "cpu_count_logical": psutil.cpu_count(logical=True),
            "cpu_count_physical": psutil.cpu_count(logical=False),
            "ram_total_gb": round(memory.total / (1024 ** 3), 2),
            "ram_available_gb": round(memory.available / (1024 ** 3), 2),
            "disk_root": str(root),
            "disk_total_gb": round(disk.total / (1024 ** 3), 2),
            "disk_free_gb": round(disk.free / (1024 ** 3), 2),
            "python": platform.python_version(),
        }

    @classmethod
    def _system_status(cls) -> dict[str, Any]:
        psutil = cls._psutil()
        status = cls._system_info()
        status.update({
            "cpu_percent": round(float(psutil.cpu_percent(interval=0.15)), 1),
            "ram_used_percent": round(float(psutil.virtual_memory().percent), 1),
            "uptime_seconds": max(0, int(time.time() - psutil.boot_time())),
            "battery": cls._battery(),
        })
        return status

    @staticmethod
    def _gb(value: Any) -> str:
        return f"{float(value):.1f} GB"

    @classmethod
    def _info_text(cls, data: dict[str, Any]) -> str:
        return (
            f"System: {data['os']} {data['os_release']} ({data['machine']}). "
            f"CPU: {data['processor'] or 'unknown'}, {data['cpu_count_logical']} logical cores. "
            f"RAM: {cls._gb(data['ram_available_gb'])} available of {cls._gb(data['ram_total_gb'])}. "
            f"Disk {data['disk_root']}: {cls._gb(data['disk_free_gb'])} free of {cls._gb(data['disk_total_gb'])}. "
            f"Python {data['python']}."
        )

    @classmethod
    def _status_text(cls, data: dict[str, Any]) -> str:
        up = int(data.get("uptime_seconds") or 0)
        hours, rem = divmod(up, 3600)
        minutes = rem // 60
        return (
            f"System status: CPU {data['cpu_percent']:.1f}%, RAM {data['ram_used_percent']:.1f}% used, "
            f"uptime {hours}h {minutes}m, and {cls._gb(data['disk_free_gb'])} free on {data['disk_root']}. "
            f"{cls._battery_text(data['battery'])}"
        )

    @staticmethod
    def _battery_text(data: dict[str, Any]) -> str:
        if not data.get("available"):
            return "No battery is exposed by the operating system."
        state = "charging" if data.get("charging") else ("plugged in" if data.get("plugged_in") else "on battery")
        return f"Battery: {data['percent']:.0f}% ({state})."
