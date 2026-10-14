from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class MCPConfigurationError(ValueError):
    """Raised when the optional MCP configuration is malformed."""


class _AsyncMCPRuntime:
    def __init__(self, configs: dict[str, dict[str, Any]]) -> None:
        self.configs = configs
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._clients: dict[str, Any] = {}
        self._stack: AsyncExitStack | None = None
        self._start_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._closed = False

    def _ensure_started(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("MCP runtime is closed.")
            if self._thread is not None and self._thread.is_alive():
                return
        with self._start_lock:
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("MCP runtime is closed.")
                if self._thread is not None and self._thread.is_alive():
                    return
                self._ready.clear()
                self._thread = threading.Thread(target=self._thread_main, name="advi-mcp", daemon=True)
                self._thread.start()
            if not self._ready.wait(timeout=5):
                raise RuntimeError("MCP runtime thread did not start.")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        stack = AsyncExitStack()
        loop.run_until_complete(stack.__aenter__())
        with self._state_lock:
            self._loop = loop
            self._stack = stack
        self._ready.set()

        # close() may race with startup after _ready is signalled but before the
        # event loop enters run_forever(). Re-check the lifecycle state here so
        # that such a close can never leave an immortal loop thread behind.
        with self._state_lock:
            closed = self._closed
        if closed:
            try:
                self._run_close_sync(loop)
            finally:
                loop.close()
            return

        try:
            loop.run_forever()
        finally:
            try:
                self._run_close_sync(loop)
            finally:
                loop.close()

    async def _close_async(self) -> None:
        self._clients.clear()
        if self._stack is not None:
            await self._stack.__aexit__(None, None, None)
            self._stack = None

    def _run_close_sync(self, loop: asyncio.AbstractEventLoop) -> None:
        """Run shutdown on the worker loop without leaking a coroutine on races."""
        close_coro = self._close_async()
        try:
            loop.run_until_complete(close_coro)
        except Exception:
            close_coro.close()
            raise

    def submit(self, coro: Any, timeout: float = 30.0) -> Any:
        self._ensure_started()
        with self._state_lock:
            if self._closed or self._loop is None:
                raise RuntimeError("MCP runtime is closed.")
            loop = self._loop
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise RuntimeError(f"MCP operation timed out after {timeout:.1f}s.")

    async def _get_client(self, server_name: str) -> Any:
        if server_name in self._clients:
            return self._clients[server_name]
        config = self.configs.get(server_name)
        if config is None:
            raise KeyError(f"Unknown MCP server: {server_name}")
        try:
            from mcp import Client, StdioServerParameters
        except ImportError as exc:
            raise RuntimeError("The optional MCP SDK is not installed. Run the ADVI setup again.") from exc

        transport = str(config.get("transport", "stdio")).strip().lower()
        if transport == "stdio":
            command = str(config.get("command", "")).strip()
            if not command:
                raise MCPConfigurationError(f"MCP server '{server_name}' is missing command.")
            params = StdioServerParameters(
                command=command,
                args=[str(x) for x in config.get("args", [])],
                env={str(k): str(v) for k, v in (config.get("env") or {}).items()},
            )
            client_arg = params
        elif transport in {"http", "streamable_http", "streamable-http", "sse"}:
            url = str(config.get("url", "")).strip()
            if not url:
                raise MCPConfigurationError(f"MCP server '{server_name}' is missing url.")
            client_arg = url
        else:
            raise MCPConfigurationError(f"Unsupported MCP transport for '{server_name}': {transport}")

        if self._stack is None:
            raise RuntimeError("MCP runtime is not initialized.")
        client = await self._stack.enter_async_context(Client(client_arg))
        self._clients[server_name] = client
        return client

    async def _list_tools(self, server_name: str) -> list[dict[str, Any]]:
        client = await self._get_client(server_name)
        result = await client.list_tools()
        rows: list[dict[str, Any]] = []
        for tool in getattr(result, "tools", []) or []:
            schema = getattr(tool, "inputSchema", None)
            if schema is None:
                schema = getattr(tool, "input_schema", None)
            if hasattr(schema, "model_dump"):
                schema = schema.model_dump()
            rows.append({
                "name": str(getattr(tool, "name", "")),
                "description": str(getattr(tool, "description", "") or ""),
                "input_schema": schema if isinstance(schema, dict) else {},
            })
        return rows

    async def _call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = await self._get_client(server_name)
        result = await client.call_tool(tool_name, arguments)
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            try:
                output = structured.model_dump() if hasattr(structured, "model_dump") else structured
            except Exception:
                output = str(structured)
        else:
            output_parts: list[Any] = []
            for item in getattr(result, "content", []) or []:
                text = getattr(item, "text", None)
                if text is not None:
                    output_parts.append(str(text))
                elif hasattr(item, "model_dump"):
                    output_parts.append(item.model_dump())
                else:
                    output_parts.append(str(item))
            output = output_parts[0] if len(output_parts) == 1 else output_parts
        is_error = bool(getattr(result, "is_error", getattr(result, "isError", False)))
        return {"output": output, "is_error": is_error}

    def close(self) -> None:
        """Request worker shutdown; the worker owns async resource cleanup.

        The caller never constructs ``_close_async()``. This is intentional: if
        the event loop stops between the state check and scheduling, there is no
        coroutine object left behind for Python to report as never awaited.
        """
        with self._state_lock:
            if self._closed and self._loop is None and self._thread is None:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread

        if thread is None:
            return
        if loop is None:
            self._ready.wait(timeout=5)
            with self._state_lock:
                loop = self._loop
                thread = self._thread
        if loop is None or thread is None:
            return

        try:
            # Only enqueue a synchronous callback. The worker thread will create
            # and await _close_async() from its own event loop in _thread_main().
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
            if thread is not threading.current_thread():
                thread.join(timeout=5)
        except Exception as exc:
            logger.debug("MCP runtime close failed: %s", exc)
        finally:
            with self._state_lock:
                self._loop = None
                self._thread = None
                self._ready.clear()


class MCPManager:
    """Thin optional client for configured MCP servers.

    ADVI owns the user interaction and task lifecycle; this class only owns MCP
    transport/session management and exact tool invocation.
    """

    def __init__(self, config_path: Path | str | None = None) -> None:
        self.config_path = Path(config_path) if config_path else Path.cwd() / "mcp_servers.json"
        self.configs = self._load_config(self.config_path)
        self._runtime = _AsyncMCPRuntime(self.configs) if self.configs else None
        self._prepared = False
        self._server_status: dict[str, str] = {}
        self._tool_cache: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def _load_config(path: Path) -> dict[str, dict[str, Any]]:
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise MCPConfigurationError(f"Could not read MCP configuration: {exc}") from exc
        servers = raw.get("servers") if isinstance(raw, dict) else None
        if not isinstance(servers, dict):
            raise MCPConfigurationError("MCP configuration must contain a 'servers' object.")
        configs: dict[str, dict[str, Any]] = {}
        for name, config in servers.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(config, dict):
                continue
            configs[name.strip()] = dict(config)
        return configs

    def is_available(self) -> bool:
        if not self.configs:
            return False
        try:
            import mcp  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def prepared(self) -> bool:
        return self._prepared

    @property
    def server_status(self) -> dict[str, str]:
        return dict(self._server_status)

    def prepare(self, *, timeout_seconds: float = 12.0) -> bool:
        """Connect to configured MCP servers and cache their initial tool lists.

        MCP is optional, so one broken server must not prevent ADVI from starting
        when other core capabilities are usable. Successful servers are cached and
        can be used immediately when the first MCP request arrives.
        """
        if not self.configs or not self.is_available() or self._runtime is None:
            return False
        self._runtime._ensure_started()
        ready = 0
        for name in self.configs:
            try:
                tools = self._runtime.submit(self._runtime._list_tools(name), timeout=max(2.0, float(timeout_seconds)))
                for tool in tools:
                    tool["server"] = name
                self._tool_cache[name] = tools
                self._server_status[name] = "ready"
                ready += 1
            except Exception as exc:
                self._server_status[name] = f"unavailable: {exc}"
                logger.warning("MCP server '%s' startup preparation failed: %s", name, exc)
        self._prepared = True
        return ready > 0

    def list_tools(self, server: str | None = None) -> list[dict[str, Any]]:
        if not self.configs:
            return []
        if not self.is_available():
            raise RuntimeError("MCP is configured but the MCP SDK is not installed.")
        assert self._runtime is not None
        names = [server] if server else list(self.configs)
        rows: list[dict[str, Any]] = []
        for name in names:
            cached = self._tool_cache.get(name) if self._prepared else None
            tools = list(cached) if cached is not None else self._runtime.submit(self._runtime._list_tools(name))
            for tool in tools:
                tool = dict(tool)
                tool["server"] = name
                rows.append(tool)
        return rows

    def call_tool(self, server: str, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configs:
            raise RuntimeError("No MCP servers are configured. Add mcp_servers.json first.")
        if not self.is_available():
            raise RuntimeError("MCP is configured but the MCP SDK is not installed.")
        server = str(server or "").strip()
        tool = str(tool or "").strip()
        if server not in self.configs:
            raise KeyError(f"Unknown MCP server: {server}")
        if not tool:
            raise ValueError("MCP tool name is required.")
        assert self._runtime is not None
        result = self._runtime.submit(self._runtime._call_tool(server, tool, dict(arguments or {})))
        result["server"] = server
        result["tool"] = tool
        return result

    def close(self) -> None:
        if self._runtime is not None:
            self._runtime.close()
