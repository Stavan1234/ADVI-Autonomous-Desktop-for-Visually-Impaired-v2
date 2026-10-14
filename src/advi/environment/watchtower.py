from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
import re
import subprocess
from pathlib import Path
import threading
import time
from typing import Any, Iterable

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - dependency is installed in production environments
    FileSystemEventHandler = object  # type: ignore[misc,assignment]
    Observer = None  # type: ignore[assignment,misc]

from .environment import EnvironmentPaths, get_environment_paths

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnvironmentEvent:
    kind: str
    path: str
    dest_path: str | None = None
    is_directory: bool = False
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True)
class ApplicationRecord:
    """Authoritative local identity record for one discoverable application.

    The display name is intentionally not the identity.  Packaged Windows apps
    and PWAs may have no conventional executable, while classic Win32 apps may
    have a process identity but no useful AppID.  Keep all known identity
    anchors together so launch and window binding can use the strongest one.
    """

    name: str
    source: str
    executable: str | None = None
    command: str | None = None
    path: str | None = None
    app_id: str | None = None
    package_family: str | None = None
    runtime_type: str = "unknown"

    @property
    def identity_key(self) -> str:
        if self.app_id:
            return f"aumid:{self.app_id}"
        if self.command and str(self.command).casefold().startswith("shell:appsfolder\\"):
            return f"command:{self.command}"
        if self.executable:
            return f"exe:{self.executable}"
        if self.path:
            return f"path:{self.path}"
        return f"name:{self.name.casefold()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "executable": self.executable,
            "command": self.command,
            "path": self.path,
            "app_id": self.app_id,
            "package_family": self.package_family,
            "runtime_type": self.runtime_type,
            "identity_key": self.identity_key,
        }


@dataclass
class EnvironmentSnapshot:
    paths: EnvironmentPaths
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    applications: dict[str, ApplicationRecord] = field(default_factory=dict)
    running_processes: set[str] = field(default_factory=set)
    events: list[EnvironmentEvent] = field(default_factory=list)
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def record_event(self, event: EnvironmentEvent, max_events: int = 200) -> None:
        self.events.append(event)
        if len(self.events) > max_events:
            del self.events[:-max_events]

    def summary_for_prompt(self) -> str:
        recent = self.events[-10:]
        recent_text = "\n".join(
            f"- {e.kind}: {e.path}" + (f" -> {e.dest_path}" if e.dest_path else "")
            for e in recent
        ) or "None"
        app_names = sorted((app.name for app in self.applications.values()), key=str.casefold)[:20]
        process_names = sorted(self.running_processes, key=str.casefold)[:30]
        recent_files = [
            meta.get("path")
            for meta in list(self.files.values())[-20:]
            if meta.get("path")
        ]
        return (
            "Environment facts (observed locally; not inferred):\n"
            f"- User home: {self.paths.user_home}\n"
            f"- Desktop: {self.paths.desktop}\n"
            f"- Documents: {self.paths.documents}\n"
            f"- Downloads: {self.paths.downloads}\n"
            f"- Known applications: {len(self.applications)}\n"
            f"- Application names (sample): {', '.join(app_names) or 'None'}\n"
            f"- Running processes observed: {len(self.running_processes)}\n"
            f"- Running process names (sample): {', '.join(process_names) or 'None'}\n"
            f"- Recently indexed files (sample): {', '.join(str(p) for p in recent_files[-20:]) or 'None'}\n"
            f"- Recent environment changes:\n{recent_text}"
        )


class _ChangeHandler(FileSystemEventHandler):
    def __init__(self, owner: "EnvironmentWatchtower") -> None:
        self.owner = owner

    def on_created(self, event) -> None:  # type: ignore[no-untyped-def]
        self.owner.record_event("created", event.src_path, event.is_directory)

    def on_modified(self, event) -> None:  # type: ignore[no-untyped-def]
        self.owner.record_event("modified", event.src_path, event.is_directory)

    def on_deleted(self, event) -> None:  # type: ignore[no-untyped-def]
        self.owner.record_event("deleted", event.src_path, event.is_directory)

    def on_moved(self, event) -> None:  # type: ignore[no-untyped-def]
        self.owner.record_event("moved", event.src_path, event.is_directory, event.dest_path)


class EnvironmentWatchtower:
    """Local environment inventory + change watcher.

    Design goals:
    - Start observing immediately; a full-disk inventory is optional and bounded.
    - Maintain metadata only, not file contents.
    - Keep filesystem, installed applications, and running-process state separate.
    - Use periodic process reconciliation because process changes are not filesystem events.
    """

    def __init__(
        self,
        paths: EnvironmentPaths | None = None,
        roots: Iterable[Path] | None = None,
        inventory_enabled: bool = True,
        full_filesystem_inventory: bool = False,
        max_inventory_entries_per_root: int = 20_000,
        process_poll_seconds: float = 10.0,
    ) -> None:
        self.paths = paths or get_environment_paths()
        self.roots = [Path(p) for p in roots] if roots is not None else self._default_roots()
        self.inventory_enabled = inventory_enabled
        self.full_filesystem_inventory = full_filesystem_inventory
        self.max_inventory_entries_per_root = max(1_000, max_inventory_entries_per_root)
        self.process_poll_seconds = max(2.0, process_poll_seconds)
        self.snapshot = EnvironmentSnapshot(paths=self.paths)
        self._observer: Observer | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._last_process_reconcile = 0.0
        self._last_targeted_reconcile = 0.0
        self._known_roots = {str(p).casefold(): p for p in self.roots}

    def start(self) -> None:
        if self._observer is not None:
            return
        if Observer is None:
            logger.warning("watchdog is not installed; environment change watching is disabled")
            return

        if self.inventory_enabled:
            self._inventory_applications()
            self._inventory_filesystem_roots()

        observer = Observer()
        handler = _ChangeHandler(self)
        scheduled = 0
        for root in self.roots:
            try:
                if root.exists() and root.is_dir():
                    observer.schedule(handler, str(root), recursive=True)
                    scheduled += 1
            except Exception:
                logger.exception("Failed to watch environment root: %s", root)

        observer.start()
        self._observer = observer
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._reconcile_loop,
            name="advi-watchtower",
            daemon=True,
        )
        self._thread.start()
        logger.info("Environment watchtower started: roots=%d, watched=%d", len(self.roots), scheduled)

    def stop(self) -> None:
        observer = self._observer
        self._observer = None
        self._stop.set()
        if observer is not None:
            observer.stop()
            observer.join(timeout=5.0)
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        logger.info("Environment watchtower stopped")

    def record_event(
        self,
        kind: str,
        path: str,
        is_directory: bool,
        dest_path: str | None = None,
    ) -> None:
        event = EnvironmentEvent(
            kind=kind,
            path=str(path),
            dest_path=str(dest_path) if dest_path else None,
            is_directory=bool(is_directory),
        )
        with self._lock:
            self.snapshot.record_event(event)
            self._apply_file_event(event)
            self._maybe_refresh_applications(event)

    def _windows_search_filename(self, name: str, roots: list[Path], limit: int) -> list[str]:
        """Query the Windows Search index before falling back to filesystem crawling."""
        if os.name != "nt" or not str(name or "").strip():
            return []
        escaped_name = str(name).replace("'", "''")
        results: list[str] = []
        scope_sql = ""
        if len(roots) == 1:
            try:
                scope_uri = roots[0].resolve(strict=False).as_uri()
                scope_sql = " AND SCOPE='" + scope_uri.replace("'", "''") + "'"
            except (OSError, ValueError):
                scope_sql = ""
        query = (
            "SELECT TOP {limit} System.ItemPathDisplay FROM SystemIndex "
            "WHERE System.ItemName = '{name}'{scope} ORDER BY System.DateModified DESC"
        ).format(limit=max(1, min(limit, 100)), name=escaped_name, scope=scope_sql)
        script = """
$ErrorActionPreference = "Stop"
$q = $env:ADVI_SEARCH_SQL
$conn = New-Object -ComObject ADODB.Connection
$conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
$cmd = New-Object -ComObject ADODB.Command
$cmd.ActiveConnection = $conn
$cmd.CommandText = $q
$rs = $cmd.Execute()
while (-not $rs.EOF) {
    $v = [string]$rs.Fields.Item("System.ItemPathDisplay").Value
    if ($v) { [Console]::WriteLine($v) }
    $rs.MoveNext()
}
$rs.Close()
$conn.Close()
"""
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=8.0, check=False,
                env={**os.environ, "ADVI_SEARCH_SQL": query},
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if completed.returncode != 0:
            logger.debug("Windows Search query failed: %s", completed.stderr.strip())
            return []
        for line in completed.stdout.splitlines():
            candidate = str(line).strip()
            if not candidate:
                continue
            try:
                if Path(candidate).is_file():
                    results.append(candidate)
            except OSError:
                continue
            if len(results) >= limit:
                break
        return results

    def find_files_by_name(self, name: str, limit: int = 20, scope: str = "user") -> list[str]:
        """Find exact filenames using the live cache, Windows Search, then a bounded crawl."""
        needle = str(name or "").casefold().strip()
        if not needle:
            return []
        limit = max(1, min(int(limit), 50))
        scope = str(scope or "user").casefold().strip()

        if scope == "desktop":
            roots = [self.paths.desktop]
        elif scope in {"documents", "document"}:
            roots = [self.paths.documents]
        elif scope in {"downloads", "download"}:
            roots = [self.paths.downloads]
        elif scope in {"computer", "all", "disk"}:
            roots = self._search_roots()
        else:
            roots = [self.paths.user_home]

        def in_scope(path: str) -> bool:
            try:
                resolved = Path(path).resolve(strict=False)
                return any(
                    resolved == root.resolve(strict=False) or root.resolve(strict=False) in resolved.parents
                    for root in roots
                )
            except OSError:
                return False

        with self._lock:
            cached = [
                path for path, meta in self.snapshot.files.items()
                if str(meta.get("name", "")).casefold() == needle
            ]
        scoped_cached = [path for path in cached if in_scope(path)]
        if scoped_cached:
            return scoped_cached[:limit]

        indexed = self._windows_search_filename(name, roots, limit)
        if indexed:
            return list(dict.fromkeys(indexed))[:limit]

        deadline = time.monotonic() + 8.0
        found: list[str] = []
        seen: set[str] = set()
        for root in roots:
            if time.monotonic() >= deadline:
                break
            try:
                for base, dirs, files in os.walk(root, topdown=True, followlinks=False):
                    if time.monotonic() >= deadline:
                        break
                    dirs[:] = [d for d in dirs if d not in {"$Recycle.Bin", "System Volume Information"}]
                    for filename in files:
                        if filename.casefold() != needle:
                            continue
                        full = str(Path(base) / filename)
                        key = full.casefold()
                        if key in seen:
                            continue
                        seen.add(key)
                        found.append(full)
                        if len(found) >= limit:
                            return found
            except (OSError, PermissionError):
                continue
        return found

    def _search_roots(self) -> list[Path]:
        if os.name != "nt":
            return [self.paths.user_home]
        try:
            import psutil
            roots = []
            for partition in psutil.disk_partitions(all=False):
                mountpoint = Path(partition.mountpoint)
                fstype = str(getattr(partition, "fstype", "")).casefold()
                opts = str(getattr(partition, "opts", "")).casefold()
                if "cdrom" in fstype or "cdrom" in opts:
                    continue
                roots.append(mountpoint)
            if roots:
                return roots
        except Exception:
            logger.debug("Could not enumerate disk roots for whole-computer search", exc_info=True)
        return [self.paths.user_home]

    @staticmethod
    def _normalize_application_query(value: str) -> str:
        text = str(value or "").casefold().replace(".exe", "").replace("_", " ")
        # User-facing application names often contain conversational suffixes:
        # "WhatsApp or similar", "Calculator app", "Word application", etc.
        # Strip those wrappers while preserving the actual product name.
        text = re.sub(r"^\s*(?:something|anything)\s+like\s+", "", text)
        text = re.sub(r"\s+(?:or\s+)?(?:something\s+)?similar(?:\s+(?:app|application|program))?\s*$", " ", text)
        text = re.sub(r"\b(?:desktop|application|app|program)\b", " ", text)
        return " ".join(text.split())

    def _find_running_application(self, normalized_query: str) -> ApplicationRecord | None:
        if os.name != "nt" or not normalized_query:
            return None
        try:
            import psutil
        except ImportError:
            return None
        query_compact = normalized_query.replace(" ", "")
        try:
            for proc in psutil.process_iter(["name", "exe"]):
                try:
                    proc_name = str(proc.info.get("name") or "").strip()
                    if not proc_name:
                        continue
                    proc_norm = self._normalize_application_query(proc_name)
                    if query_compact not in proc_norm.replace(" ", "") and proc_norm.replace(" ", "") not in query_compact:
                        continue
                    exe = str(proc.info.get("exe") or "").strip() or None
                    return ApplicationRecord(
                        name=proc_name.removesuffix(".exe"),
                        source="running_process",
                        executable=exe,
                        runtime_type="win32",
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception:
            logger.debug("Running-process application discovery failed", exc_info=True)
        return None

    def _query_windows_start_apps(self, normalized_query: str) -> ApplicationRecord | None:
        """Ask Windows directly for a Start Apps match without relying on the cached inventory."""
        if os.name != "nt" or not normalized_query:
            return None
        try:
            escaped = normalized_query.replace("'", "''")
            command = (
                "$q = '" + escaped + "'; "
                "Get-StartApps | Where-Object { $_.Name -and (($_.Name -replace '[^A-Za-z0-9]', '').ToLower().Contains(($q -replace '[^A-Za-z0-9]', '').ToLower()) -or (($q -replace '[^A-Za-z0-9]', '').ToLower()).Contains(($_.Name -replace '[^A-Za-z0-9]', '').ToLower())) } | "
                "Select-Object -First 1 Name,AppID | ConvertTo-Json -Compress"
            )
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, timeout=6, check=False,
            )
            if completed.returncode != 0 or not completed.stdout.strip():
                return None
            import json
            row = json.loads(completed.stdout)
            if not isinstance(row, dict):
                return None
            app_name = str(row.get("Name") or "").strip()
            app_id = str(row.get("AppID") or "").strip()
            if not app_name or not app_id:
                return None
            package_family = app_id.split("!", 1)[0] if "!" in app_id else None
            return ApplicationRecord(
                name=app_name,
                source="windows_start_apps_on_demand",
                command=f"shell:AppsFolder\\{app_id}",
                app_id=app_id,
                package_family=package_family,
                runtime_type=self._classify_start_app_runtime(app_name, app_id),
            )
        except Exception:
            logger.debug("On-demand Windows Start Apps query failed", exc_info=True)
            return None

    def _query_appx_application(self, name: str) -> ApplicationRecord | None:
        """Find a current-user MSIX/AppX application that StartApps omitted."""
        if os.name != "nt":
            return None
        query = str(name or "").strip()
        if not query:
            return None
        try:
            completed = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    (
                        "$q=$env:ADVI_APP_QUERY; "
                        "$rows=Get-AppxPackage -PackageTypeFilter Main | "
                        "Where-Object { $_.Name -like ('*'+$q+'*') -or $_.PackageFamilyName -like ('*'+$q+'*') }; "
                        "$out=@(); foreach($p in $rows){ try { $m=Get-AppxPackageManifest -Package $p.PackageFullName; "
                        "foreach($a in $m.Package.Applications.Application){ $id=[string]$a.Id; if($id){ "
                        "$out += [pscustomobject]@{Name=[string]$p.Name; PFN=[string]$p.PackageFamilyName; AppId=$id} } } } catch {} }; "
                        "$out | ConvertTo-Json -Compress"
                    )
                ],
                capture_output=True, text=True, timeout=12, check=False,
                env={**os.environ, "ADVI_APP_QUERY": query},
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug("On-demand AppX application query failed", exc_info=True)
            return None
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        try:
            import json
            payload = json.loads(completed.stdout)
        except (ValueError, json.JSONDecodeError):
            return None
        rows = payload if isinstance(payload, list) else [payload]
        normalized = self._normalize_application_query(query)
        for row in rows:
            if not isinstance(row, dict):
                continue
            package_name = str(row.get("Name") or "").strip()
            pfn = str(row.get("PFN") or "").strip()
            app_id = str(row.get("AppId") or "").strip()
            if not package_name or not pfn or not app_id:
                continue
            if normalized and normalized not in self._normalize_application_query(package_name):
                continue
            aumid = f"{pfn}!{app_id}"
            return ApplicationRecord(
                name=package_name,
                source="appx_on_demand",
                command=f"shell:AppsFolder\\{aumid}",
                app_id=aumid,
                package_family=pfn,
                runtime_type="packaged",
            )
        return None

    def _application_record_is_running(self, app: ApplicationRecord) -> bool:
        """Return current process state for one inventory record without invoking GUI logic."""
        try:
            executable_name = Path(str(app.executable).split(",", 1)[0]).name.casefold() if app.executable else ""
            app_name = self._normalize_application_query(app.name)
            processes = self.get_snapshot().running_processes
            return bool((executable_name and executable_name in processes) or (app_name and app_name in processes))
        except Exception:
            return False

    def find_application(self, name: str) -> ApplicationRecord | None:
        needle = str(name or "").casefold().strip()
        normalized = self._normalize_application_query(needle)
        if not normalized:
            return None

        with self._lock:
            direct = self.snapshot.applications.get(needle)
            if direct:
                return direct
            apps = list(self.snapshot.applications.values())

        for app in apps:
            app_name = self._normalize_application_query(app.name)
            if app_name == normalized:
                return app

        # Fuzzy token/substring matching handles natural names such as
        # "WhatsApp Desktop" -> Start Apps entry "WhatsApp".
        candidates = [
            app for app in apps
            if normalized in self._normalize_application_query(app.name)
            or self._normalize_application_query(app.name) in normalized
        ]
        if candidates:
            def score(app: ApplicationRecord) -> tuple[int, int, int, int, int, str]:
                app_norm = self._normalize_application_query(app.name)
                stem = self._normalize_application_query(Path(str(app.executable).split(",",1)[0]).stem) if app.executable else ""
                tokens = set(app_norm.split())
                query_tokens = set(normalized.split())
                exact = 0 if app_norm == normalized or stem == normalized else 1
                token_match = 0 if query_tokens and query_tokens.issubset(tokens) else 1
                prefix = 0 if app_norm.startswith(normalized) or stem.startswith(normalized) else 1
                running = 0 if self._application_record_is_running(app) else 1
                length_penalty = abs(len(app_norm) - len(normalized))
                return (exact, token_match, prefix, running, length_penalty, app.name.casefold())
            return sorted(candidates, key=score)[0]

        # Applications may be installed after ADVI startup or registered by a
        # Store/MSIX package that was not visible during the initial inventory.
        # Query Windows directly first; then refresh the cache as a second chance.
        if os.name == "nt":
            direct = self._query_windows_start_apps(normalized)
            if direct is not None:
                return direct
            direct = self._query_appx_application(normalized)
            if direct is not None:
                return direct
            try:
                self.refresh_applications()
                with self._lock:
                    refreshed = list(self.snapshot.applications.values())
                for app in refreshed:
                    if self._normalize_application_query(app.name) == normalized:
                        return app
                candidates = [
                    app for app in refreshed
                    if normalized in self._normalize_application_query(app.name)
                    or self._normalize_application_query(app.name) in normalized
                ]
                if candidates:
                    def refreshed_score(app: ApplicationRecord) -> tuple[int, int, int, int, int, str]:
                        app_norm = self._normalize_application_query(app.name)
                        stem = self._normalize_application_query(Path(str(app.executable).split(",",1)[0]).stem) if app.executable else ""
                        tokens = set(app_norm.split())
                        query_tokens = set(normalized.split())
                        exact = 0 if app_norm == normalized or stem == normalized else 1
                        token_match = 0 if query_tokens and query_tokens.issubset(tokens) else 1
                        prefix = 0 if app_norm.startswith(normalized) or stem.startswith(normalized) else 1
                        running = 0 if self._application_record_is_running(app) else 1
                        length_penalty = abs(len(app_norm) - len(normalized))
                        return (exact, token_match, prefix, running, length_penalty, app.name.casefold())
                    return sorted(candidates, key=refreshed_score)[0]
            except Exception:
                logger.debug("On-demand application inventory refresh failed", exc_info=True)

            return self._find_running_application(normalized)
        return None

    def list_applications(self, *, limit: int = 120, query: str = "") -> list[dict[str, Any]]:
        """Return a bounded compact application catalog with optional name filtering."""
        limit = max(1, min(int(limit), 250))
        # Keep running-state information current even when the background poll
        # has not reached its next interval yet.
        try:
            self._reconcile_running_processes()
        except Exception:
            logger.debug("Application list process refresh failed", exc_info=True)
        query = str(query or "").strip().casefold()
        snapshot = self.get_snapshot()
        process_names = snapshot.running_processes
        rows: list[dict[str, Any]] = []
        for record in sorted(snapshot.applications.values(), key=lambda item: item.name.casefold()):
            if query and query not in record.name.casefold():
                continue
            executable_name = ""
            if record.executable:
                executable_name = Path(record.executable.split(",", 1)[0]).name.casefold()
            normalized_name = record.name.casefold().replace(".exe", "")
            running = bool(
                (executable_name and executable_name in process_names)
                or normalized_name in process_names
            )
            # Keep the provider-facing inventory compact. Launch metadata remains
            # local and is resolved by find_application/open_application.
            rows.append({"name": record.name, "running": running, "source": record.source})
            if len(rows) >= limit:
                break
        return rows

    def find_running_application(self, name: str) -> ApplicationRecord | None:
        record = self.find_application(name)
        if record is None:
            return None
        snapshot = self.get_snapshot()
        executable_name = Path(record.executable.split(",", 1)[0]).name.casefold() if record.executable else ""
        normalized_name = record.name.casefold().replace(".exe", "")
        if executable_name and executable_name in snapshot.running_processes:
            return record
        if normalized_name in snapshot.running_processes:
            return record
        return None

    def is_process_running(self, executable_name: str) -> bool:
        return executable_name.casefold() in self.get_snapshot().running_processes

    def refresh_applications(self) -> None:
        self._inventory_applications()

    def get_snapshot(self) -> EnvironmentSnapshot:
        with self._lock:
            return EnvironmentSnapshot(
                paths=self.snapshot.paths,
                files=dict(self.snapshot.files),
                applications=dict(self.snapshot.applications),
                running_processes=set(self.snapshot.running_processes),
                events=list(self.snapshot.events),
                observed_at=self.snapshot.observed_at,
            )

    def _reconcile_loop(self) -> None:
        while not self._stop.wait(self.process_poll_seconds):
            self._reconcile_running_processes()
            now = time.monotonic()
            # Notifications are the fast path. A periodic targeted reconciliation
            # repairs missed/coalesced filesystem events without crawling the whole
            # machine on every turn.
            if now - self._last_targeted_reconcile >= max(30.0, self.process_poll_seconds * 3):
                try:
                    self._targeted_reconcile()
                except Exception:
                    logger.exception("Periodic environment reconciliation failed")
                self._last_targeted_reconcile = now
            self._refresh_drive_roots()

    def _targeted_reconcile(self) -> None:
        with self._lock:
            configured_roots = list(self.roots)
        roots = configured_roots if self.full_filesystem_inventory else [
            self.paths.desktop, self.paths.documents, self.paths.downloads
        ]
        for root in roots:
            self._index_tree(root, max_entries=min(5_000, self.max_inventory_entries_per_root))
        self._inventory_applications()

    def _refresh_drive_roots(self) -> None:
        if os.name != "nt":
            return
        current = {str(p).casefold(): p for p in self._default_roots()}
        with self._lock:
            known_roots = set(self._known_roots)
        new_roots = [p for key, p in current.items() if key not in known_roots]
        if not new_roots or self._observer is None:
            return
        handler = _ChangeHandler(self)
        for root in new_roots:
            try:
                if root.exists() and root.is_dir():
                    self._observer.schedule(handler, str(root), recursive=True)
                    self.roots.append(root)
                    self._known_roots[str(root).casefold()] = root
                    logger.info("Environment watchtower added new volume root: %s", root)
            except Exception:
                logger.exception("Failed to watch newly detected root: %s", root)

    def _reconcile_running_processes(self) -> None:
        processes = self._list_running_processes()
        with self._lock:
            self.snapshot.running_processes = processes
            self._last_process_reconcile = time.monotonic()

    def refresh_runtime_state(self) -> None:
        """Synchronize current process/application state on demand.

        Filesystem watching stays scoped to configured roots, but process state is
        cheap enough to refresh across the whole Windows session whenever a user
        asks for the current application state.
        """
        self._reconcile_running_processes()
        try:
            self._inventory_applications()
        except Exception:
            logger.debug("On-demand application refresh failed", exc_info=True)

    def _list_running_processes(self) -> set[str]:
        try:
            import psutil
        except ImportError:
            logger.warning("psutil is not installed; process inventory is disabled")
            return set()
        processes: set[str] = set()
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info.get("name")
                if name:
                    processes.add(str(name).casefold())
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return processes

    def _inventory_applications(self) -> None:
        records: dict[str, ApplicationRecord] = {}
        records.update(self._discover_start_menu_apps())
        if os.name == "nt":
            records.update(self._discover_registry_apps())
            records.update(self._discover_appx_packages())
            records.update(self._discover_windows_start_apps())
        with self._lock:
            self.snapshot.applications = records
            self.snapshot.running_processes = self._list_running_processes()

    def _discover_start_menu_apps(self) -> dict[str, ApplicationRecord]:
        roots = [
            self.paths.startup.parent,
            Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        ]
        records: dict[str, ApplicationRecord] = {}
        seen: set[str] = set()
        for root in roots:
            try:
                if not root.exists():
                    continue
                for path in root.rglob("*.lnk"):
                    name = path.stem.strip()
                    key = name.casefold()
                    if not name or key in seen:
                        continue
                    seen.add(key)
                    records[key] = ApplicationRecord(
                        name=name,
                        source="start_menu",
                        path=str(path),
                        runtime_type="shortcut",
                    )
            except (OSError, PermissionError):
                logger.debug("Unable to inventory Start Menu root: %s", root)
        return records

    @staticmethod
    def _classify_start_app_runtime(name: str, app_id: str) -> str:
        """Classify common Start Apps registrations without pretending certainty.

        Chromium-installed PWAs commonly expose an AppID containing ``._crx_``.
        Everything else remains a generic packaged application unless stronger
        evidence is available elsewhere.
        """
        compact = str(app_id or "").casefold()
        if "._crx_" in compact or "webapp" in compact or "pwa" in compact:
            return "pwa"
        return "packaged"

    def _discover_windows_start_apps(self) -> dict[str, ApplicationRecord]:
        """Discover MSIX/Store apps exposed through the Windows Start Apps catalog."""
        if os.name != "nt":
            return {}
        try:
            completed = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
                ],
                capture_output=True, text=True, timeout=8, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug("Windows Start Apps discovery failed", exc_info=True)
            return {}
        if completed.returncode != 0 or not completed.stdout.strip():
            return {}
        try:
            import json
            payload = json.loads(completed.stdout)
        except (ValueError, json.JSONDecodeError):
            return {}
        rows = payload if isinstance(payload, list) else [payload]
        records: dict[str, ApplicationRecord] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("Name") or "").strip()
            app_id = str(row.get("AppID") or "").strip()
            if not name or not app_id:
                continue
            package_family = app_id.split("!", 1)[0] if "!" in app_id else None
            records[name.casefold()] = ApplicationRecord(
                name=name,
                source="windows_start_apps",
                command=f"shell:AppsFolder\\{app_id}",
                app_id=app_id,
                package_family=package_family,
                runtime_type=self._classify_start_app_runtime(name, app_id),
            )
        return records

    def _discover_appx_packages(self) -> dict[str, ApplicationRecord]:
        """Inventory current-user MSIX/AppX packages by package name."""
        if os.name != "nt":
            return {}
        try:
            completed = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "Get-AppxPackage -PackageTypeFilter Main | Select-Object Name,PackageFamilyName,InstallLocation | ConvertTo-Json -Compress",
                ],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug("AppX package discovery failed", exc_info=True)
            return {}
        if completed.returncode != 0 or not completed.stdout.strip():
            return {}
        try:
            import json
            payload = json.loads(completed.stdout)
        except (ValueError, json.JSONDecodeError):
            return {}
        rows = payload if isinstance(payload, list) else [payload]
        records: dict[str, ApplicationRecord] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("Name") or "").strip()
            if not name:
                continue
            pfn = str(row.get("PackageFamilyName") or "").strip() or None
            records[name.casefold()] = ApplicationRecord(
                name=name,
                source="appx_package",
                path=str(row.get("InstallLocation") or "") or None,
                package_family=pfn,
                runtime_type="packaged",
            )
        return records

    def _discover_registry_apps(self) -> dict[str, ApplicationRecord]:
        try:
            import winreg
        except ImportError:
            return {}

        records: dict[str, ApplicationRecord] = {}
        locations = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, base in locations:
            try:
                with winreg.OpenKey(hive, base) as root:
                    count = winreg.QueryInfoKey(root)[0]
                    for i in range(count):
                        try:
                            subkey = winreg.EnumKey(root, i)
                            with winreg.OpenKey(root, subkey) as item:
                                name = _reg_value(item, winreg, "DisplayName")
                                if not name:
                                    continue
                                executable = _reg_value(item, winreg, "DisplayIcon")
                                key = name.casefold()
                                records[key] = ApplicationRecord(
                                    name=name,
                                    source="registry",
                                    executable=executable,
                                    runtime_type="win32",
                                )
                        except OSError:
                            continue
            except OSError:
                continue
        return records

    def _inventory_filesystem_roots(self) -> None:
        with self._lock:
            configured_roots = list(self.roots)
        roots = configured_roots if self.full_filesystem_inventory else [
            self.paths.desktop, self.paths.documents, self.paths.downloads
        ]
        for root in roots:
            self._index_tree(root, max_entries=self.max_inventory_entries_per_root)

    def _index_tree(self, root: Path, max_entries: int) -> None:
        if not root.exists() or not root.is_dir():
            return
        stack = [root]
        indexed = 0
        while stack and indexed < max_entries:
            current = stack.pop()
            try:
                for child in current.iterdir():
                    if indexed >= max_entries:
                        break
                    try:
                        if child.is_dir():
                            stack.append(child)
                            continue
                        stat = child.stat()
                        self.snapshot.files[str(child)] = {
                            "path": str(child),
                            "name": child.name,
                            "size": stat.st_size,
                            "modified_ns": stat.st_mtime_ns,
                        }
                        indexed += 1
                    except (OSError, PermissionError):
                        continue
            except (OSError, PermissionError):
                continue

    def _apply_file_event(self, event: EnvironmentEvent) -> None:
        path = str(event.path)
        if event.kind in {"deleted", "moved"}:
            self.snapshot.files.pop(path, None)
        if event.kind in {"created", "modified"}:
            p = Path(path)
            if not event.is_directory:
                try:
                    stat = p.stat()
                    self.snapshot.files[path] = {
                        "path": path,
                        "name": p.name,
                        "size": stat.st_size,
                        "modified_ns": stat.st_mtime_ns,
                    }
                except OSError:
                    pass
        if event.kind == "moved" and event.dest_path and not event.is_directory:
            p = Path(event.dest_path)
            try:
                stat = p.stat()
                self.snapshot.files[event.dest_path] = {
                    "path": event.dest_path,
                    "name": p.name,
                    "size": stat.st_size,
                    "modified_ns": stat.st_mtime_ns,
                }
            except OSError:
                pass

    def _maybe_refresh_applications(self, event: EnvironmentEvent) -> None:
        normalized = str(event.path).lower()
        if "start menu" in normalized or normalized.endswith(".lnk"):
            self._inventory_applications()

    def _default_roots(self) -> list[Path]:
        if os.name != "nt":
            return [Path.home()]
        return [
            self.paths.desktop,
            self.paths.documents,
            self.paths.downloads,
            self.paths.startup.parent,
        ]


def _reg_value(key, winreg, name: str) -> str | None:  # type: ignore[no-untyped-def]
    try:
        value, _ = winreg.QueryValueEx(key, name)
        return str(value) if value else None
    except OSError:
        return None
