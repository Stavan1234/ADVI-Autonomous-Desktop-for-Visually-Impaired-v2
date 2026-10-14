from __future__ import annotations

"""ADVI Windows live Windows capability smoke test.

This intentionally avoids LLM calls and irreversible operations. It validates:
- Windows desktop/display access
- native file create/read/delete round-trip in a temporary directory
- Chrome CDP on an ADVI-owned temporary profile
- Gmail credential readiness without starting OAuth interactively
- watchdog environment events on a temporary watched directory
- capability registry readiness

Exit code is non-zero only when a required live capability fails.
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from advi.capabilities.browser.executor import BrowserCapability
from advi.capabilities.desktop.context import DesktopContext
from advi.capabilities.files.executor import FileCapability
from advi.capabilities.registry import build_default_registry, CapabilityStatus
from advi.environment import EnvironmentWatchtower, get_environment_paths
from advi.integrations.gmail.auth import CREDENTIALS_FILE, TOKEN_FILE
from advi.tools.contracts import ToolCall


class Results:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.skips: list[str] = []

    def ok(self, name: str, detail: str) -> None:
        print(f"PASS  {name}: {detail}")

    def skip(self, name: str, detail: str) -> None:
        self.skips.append(name)
        print(f"SKIP  {name}: {detail}")

    def fail(self, name: str, detail: str) -> None:
        self.failures.append(name)
        print(f"FAIL  {name}: {detail}")


def main() -> int:
    results = Results()
    print("ADVI Windows — Windows capability smoke test")
    print("No LLM calls. No email sends. No user files are modified.")
    print()

    if os.name != "nt":
        results.fail("Windows", "This smoke test must run on Windows.")
        return 1
    results.ok("Windows", f"{sys.getwindowsversion().major}.{sys.getwindowsversion().build}")

    # Desktop preflight: no clicks, keypresses, screenshots, or focus changes.
    try:
        import pyautogui
        width, height = pyautogui.size()
        if width > 0 and height > 0:
            results.ok("Desktop", f"display/input backend reachable at {width}x{height}")
        else:
            results.fail("Desktop", f"invalid display dimensions: {width}x{height}")
    except Exception as exc:
        results.fail("Desktop", f"desktop backend unavailable: {exc}")

    # Native filesystem round-trip in a dedicated temp directory.
    temp_root = Path(tempfile.mkdtemp(prefix="advi_smoke_"))
    try:
        file_cap = FileCapability()
        file_cap.desktop_dir = temp_root
        create = file_cap.execute(
            ToolCall(
                call_id="smoke-create",
                name="create_file",
                arguments={"path": "probe.txt", "content": "ADVI_PHASE8_OK"},
            )
        )
        read = file_cap.execute(
            ToolCall(
                call_id="smoke-read",
                name="read_file",
                arguments={"path": "probe.txt"},
            )
        )
        delete = file_cap.execute(
            ToolCall(
                call_id="smoke-delete",
                name="delete_file",
                arguments={"path": "probe.txt"},
            )
        )
        if create.success and create.verified and read.success and read.data == "ADVI_PHASE8_OK" and delete.success and delete.verified:
            results.ok("Filesystem", "create/read/delete native capability round-trip verified")
        else:
            results.fail(
                "Filesystem",
                f"create={create.success}/{create.verified}, read={read.success}, delete={delete.success}/{delete.verified}",
            )
    except Exception as exc:
        results.fail("Filesystem", str(exc))

    # Environment watcher: observe real OS filesystem events in a temporary root.
    watch_root = temp_root / "watch"
    watch_root.mkdir(parents=True, exist_ok=True)
    try:
        watcher = EnvironmentWatchtower(
            paths=get_environment_paths(),
            roots=[watch_root],
            inventory_enabled=False,
            full_filesystem_inventory=False,
            process_poll_seconds=2.0,
        )
        watcher.start()
        event_file = watch_root / "event.txt"
        event_file.write_text("watch", encoding="utf-8")
        deadline = time.time() + 5.0
        saw_create = False
        while time.time() < deadline:
            events = watcher.get_snapshot().events
            saw_create = any(
                e.kind == "created" and Path(e.path) == event_file
                for e in events
            )
            if saw_create:
                break
            time.sleep(0.1)
        watcher.stop()
        if saw_create:
            results.ok("Watchtower", "real filesystem create event observed")
        else:
            results.fail("Watchtower", "no create event observed within 5 seconds")
    except Exception as exc:
        results.fail("Watchtower", str(exc))

    # Chrome CDP smoke test on a dedicated ADVI profile. This may briefly launch
    # an isolated Chrome window. It never connects to the user's normal profile.
    browser_profile = temp_root / "chrome-profile"
    browser = BrowserCapability(
        context=DesktopContext(),
        cdp_port=9333,
        user_data_dir=browser_profile,
    )
    launched = False
    try:
        before_tabs = browser.cdp.list_tabs()
        if before_tabs:
            results.fail("Chrome CDP", "port 9333 is already serving a browser; refusing to attach to an unrelated session")
            ensured = False
        else:
            ensured = browser._ensure_chrome_running()
        launched = browser.context.chrome_process_id is not None
        tab_id = browser.context.target_tab_id
        if not before_tabs and (not ensured or not tab_id):
            results.fail("Chrome CDP", "could not establish an ADVI-owned target tab")
        else:
            ready = browser.cdp.evaluate_js(tab_id, "document.readyState")
            title = browser.cdp.evaluate_js(tab_id, "document.title")
            results.ok("Chrome CDP", f"owned tab={tab_id}, readyState={ready!r}, title={title!r}")
    except Exception as exc:
        results.fail("Chrome CDP", str(exc))
    finally:
        try:
            browser.close()
        except Exception as exc:
            results.fail("Chrome cleanup", str(exc))

    # Gmail: never trigger OAuth interactively. Readiness is based only on an
    # existing token/credentials file. Actual API read is a separate manual gate.
    token_exists = TOKEN_FILE.exists()
    credentials_exists = CREDENTIALS_FILE.exists()
    if not token_exists and not credentials_exists:
        results.skip("Gmail", "no existing OAuth token/credentials; interactive auth intentionally not triggered")
    elif token_exists:
        results.ok("Gmail", f"OAuth token present at {TOKEN_FILE}")
    else:
        results.skip("Gmail", "client credentials exist, but no token; OAuth flow intentionally not triggered")

    # Registry readiness should never crash even when optional services are absent.
    try:
        registry = build_default_registry(refresh=True)
        report = registry.readiness_report()
        if report.ready:
            results.ok("Capability registry", f"{report.checked_actions} actions passed readiness")
        else:
            details = "; ".join(f"{i.code}:{i.action}" for i in report.issues[:8])
            results.fail("Capability registry", f"{len(report.issues)} readiness issues: {details}")

        for capability in registry.list_all():
            snap = capability.availability_snapshot()
            print(f"      {snap.capability}: {snap.status.value} — {snap.reason}")
    except Exception as exc:
        results.fail("Capability registry", str(exc))

    shutil.rmtree(temp_root, ignore_errors=True)

    print()
    print(f"Completed: failures={len(results.failures)}, skips={len(results.skips)}")
    if results.failures:
        print("Failed checks: " + ", ".join(results.failures))
        return 1
    print("Windows smoke gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
