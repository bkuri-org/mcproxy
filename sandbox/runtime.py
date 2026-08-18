"""Runtime classes for sandbox execution.

These classes are injected into the sandbox subprocess as code strings.
They cannot import from the parent process - all classes must be self-contained.

The classes work with JSON-serialized manifest data (not live objects) and use
_IPCClient for synchronous tool execution via Unix Domain Socket IPC.

Sync-Only Execution:
- All tool calls execute immediately via Unix Domain Socket IPC
- Results are available inline during code execution
- Parent process executes tools and returns results to subprocess

Tool Inspection:
- Use .inspect() to get tool schema and metadata
- Example: schema = api.server("name").tool.inspect()
- Returns: server, name, description, inputSchema
"""

RUNTIME_CLASSES = """
import json
import os
import socket
import time


def _sanitize_for_json(obj, path="root", seen=None):
    '''Validate and sanitize objects for JSON serialization.
    
    Detects circular references and non-serializable types.
    Returns (success: bool, sanitized_obj or error_message: str)
    '''
    if seen is None:
        seen = set()
    
    obj_id = id(obj)
    if obj_id in seen:
        return False, f"Circular reference detected at {path}. Pass only primitive values (str, int, float, bool, list, dict) to MCP tools."
    
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return True, obj
    
    if isinstance(obj, (list, tuple)):
        seen.add(obj_id)
        result = []
        for i, item in enumerate(obj):
            success, value = _sanitize_for_json(item, f"{path}[{i}]", seen)
            if not success:
                return False, value
            result.append(value)
        return True, result
    
    if isinstance(obj, dict):
        seen.add(obj_id)
        result = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                return False, f"Non-string dict key at {path}: {type(k).__name__}. All dict keys must be strings."
            success, value = _sanitize_for_json(v, f"{path}.{k}", seen)
            if not success:
                return False, value
            result[k] = value
        return True, result
    
    # Non-serializable type
    return False, f"Non-serializable type at {path}: {type(obj).__name__}. Pass only primitive values (str, int, float, bool, list, dict) to MCP tools."


class _TraceCollector:
    '''Collects trace events for debugging and performance analysis.'''
    _instance = None
    
    def __init__(self):
        self._calls = []
        self._enabled = False
        self._total_tool_time_ms = 0
    
    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def enable(self):
        self._enabled = True
        self._calls = []
    
    def disable(self):
        self._enabled = False
    
    def record_call(self, server, tool, args, duration_ms, error=None):
        self._total_tool_time_ms += duration_ms
        if self._enabled:
            self._calls.append({
                "server": server,
                "tool": tool,
                "args": args,
                "duration_ms": duration_ms,
                "error": error,
            })
    
    def get_calls(self):
        return self._calls
    
    def get_total_tool_time_ms(self):
        return self._total_tool_time_ms
    
    def reset(self):
        self._calls = []
        self._total_tool_time_ms = 0


class _IPCClient:
    def __init__(self, retries=0):
        self._sock_path = os.environ.get("MCPROXY_IPC_SOCK")
        self._call_id = 0
        self._retries = retries
        self._timeout = float(os.environ.get("MCPROXY_IPC_TIMEOUT", "30.0"))

    def call(self, server, tool, args):
        if not self._sock_path:
            raise RuntimeError(
                "IPC socket not available. MCPROXY_IPC_SOCK environment variable not set."
            )

        last_error = None
        for attempt in range(self._retries + 1):
            try:
                return self._call_once(server, tool, args)
            except Exception as e:
                last_error = e
                error_msg = str(e).lower()
                
                # Only retry on timeout/network errors
                is_retryable = (
                    "timeout" in error_msg or
                    "timed out" in error_msg or
                    "connection" in error_msg or
                    "network" in error_msg
                )
                
                # Don't retry if not retryable or last attempt
                if not is_retryable or attempt == self._retries:
                    raise
                
                # Exponential backoff: 100ms, 200ms, 400ms, etc.
                delay = 0.1 * (2 ** attempt)
                time.sleep(delay)

    def _call_once(self, server, tool, args):
        call_start = time.perf_counter()
        self._call_id += 1
        
        # Sanitize args before serialization
        success, sanitized = _sanitize_for_json(args)
        if not success:
            raise ValueError(sanitized)  # sanitized contains error message
        
        request = {
            "call_id": self._call_id,
            "server": server,
            "tool": tool,
            "args": sanitized,
        }

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self._sock_path)
            sock.settimeout(self._timeout)
            sock.sendall(json.dumps(request).encode())
            sock.shutdown(socket.SHUT_WR)

            response_data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response_data += chunk

            if not response_data:
                raise RuntimeError(
                    f"IPC server returned empty response (socket: {self._sock_path}). "
                    f"The sandbox IPC server may have crashed or failed to serialize the response."
                )

            response = json.loads(response_data)
            duration_ms = int((time.perf_counter() - call_start) * 1000)

            if response.get("status") == "error":
                error = response.get("error", "Unknown IPC error")
                _TraceCollector.get().record_call(server, tool, args, duration_ms, error)
                raise RuntimeError(error)

            result = response.get("result")
            
            # Auto-unwrap MCP protocol responses: {"content": [{"type": "text", "text": "..."}]}
            if isinstance(result, dict) and "content" in result:
                content = result.get("content", [])
                if isinstance(content, list) and len(content) > 0:
                    first_item = content[0]
                    if isinstance(first_item, dict) and first_item.get("type") == "text":
                        text = first_item.get("text", "")
                        try:
                            unwrapped = json.loads(text)
                            _TraceCollector.get().record_call(server, tool, args, duration_ms)
                            return unwrapped
                        except (json.JSONDecodeError, TypeError):
                            _TraceCollector.get().record_call(server, tool, args, duration_ms)
                            return text
            
            _TraceCollector.get().record_call(server, tool, args, duration_ms)
            return result
        except Exception as e:
            duration_ms = int((time.perf_counter() - call_start) * 1000)
            _TraceCollector.get().record_call(server, tool, args, duration_ms, str(e))
            raise
        finally:
            sock.close()


class _Manifest:
    def __init__(self, data):
        self._data = data

    @property
    def servers(self):
        return self._data.get("servers", {})

    @property
    def namespaces(self):
        return self._data.get("namespaces", {})

    @property
    def groups(self):
        return self._data.get("groups", {})


class _CapabilityRegistry:
    def __init__(self, manifest):
        self.servers = manifest.servers
        self.namespaces = manifest.namespaces
        self.groups = manifest.groups

    def get_server(self, name):
        return self.servers.get(name)

    def get_namespace(self, name):
        return self.namespaces.get(name)

    def get_group(self, name):
        return self.groups.get(name)

    def get_tools_for_server(self, server_name):
        server = self.get_server(server_name)
        if not server:
            return []
        return server.get("tools", [])


class _NamespaceAccessControl:
    def __init__(self, manifest):
        self.manifest = manifest

    def can_access(self, namespace, target_server):
        # Fail closed: no namespace = no tool access. Pure code still runs;
        # only api.server(...) calls hit this check.
        if not namespace:
            available = ", ".join(sorted(self.manifest.namespaces.keys())) or "none"
            return False, (
                "No namespace specified. Pass namespace= in the dispatch call "
                f"or an X-Namespace header. Available: {available}"
            )

        allowed = self._resolve_allowed_servers(namespace)
        if target_server in allowed:
            return True, ""
        
        # Provide helpful error with available servers
        available = sorted(allowed) if allowed else ["none"]
        if len(available) <= 5:
            server_list = ", ".join(available)
        else:
            server_list = ", ".join(available[:5]) + f", and {len(available) - 5} more"
        
        return False, (
            f"Access denied to '{target_server}'. "
            f"Available servers in '{namespace}' namespace: {server_list}"
        )

    def _resolve_allowed_servers(self, namespace_or_group):
        resolved = set()
        visited = set()

        def _resolve_namespace(ns):
            if ns in visited:
                return
            visited.add(ns)
            ns_config = self.manifest.get_namespace(ns)
            if not ns_config:
                return
            resolved.update(ns_config.get("servers", []))
            for parent in ns_config.get("extends", []):
                _resolve_namespace(parent)

        group_config = self.manifest.get_group(namespace_or_group)
        if group_config:
            for ns_ref in group_config.get("namespaces", []):
                actual_ns = ns_ref[1:] if ns_ref.startswith("!") else ns_ref
                _resolve_namespace(actual_ns)
        else:
            _resolve_namespace(namespace_or_group)

        return resolved


class _ToolProxy:
    '''Proxy for a single tool, providing both call and inspect capabilities.'''
    
    def __init__(self, server_name, tool_name, ipc_client, manifest):
        self._server_name = server_name
        self._tool_name = tool_name
        self._ipc_client = ipc_client
        self._manifest = manifest
    
    def __call__(self, **kwargs):
        '''Execute the tool with keyword arguments only.

        CORRECT:   api.server('name').tool_name(param1='val1', param2='val2')
        INCORRECT: api.server('name').tool_name({'param1': 'val1'})
        INCORRECT: api.server('name').tool_name(param_dict)

        All arguments must be keyword arguments. Do not pass dicts as
        positional args. If you have a dict, unpack it: tool_name(**my_dict)
        '''
        # Try exact match first, then try with hyphens/underscores swapped
        server_info = self._manifest.servers.get(self._server_name, {})
        tools = server_info.get("tools", [])
        tool_names = [t.get("name") for t in tools]
        
        # Try exact match
        actual_tool_name = self._tool_name
        if self._tool_name not in tool_names:
            # Try replacing underscores with hyphens
            normalized = self._tool_name.replace("_", "-")
            if normalized in tool_names:
                actual_tool_name = normalized
            else:
                # Try replacing hyphens with underscores
                normalized = self._tool_name.replace("-", "_")
                if normalized in tool_names:
                    actual_tool_name = normalized
        
        return self._ipc_client.call(self._server_name, actual_tool_name, kwargs)
    
    def inspect(self):
        '''Get tool schema and metadata without executing.
        
        Returns:
            Dict with tool information including:
            - server: Server name
            - name: Tool name
            - description: Tool description
            - inputSchema: JSON Schema for tool parameters
        '''
        server_info = self._manifest.servers.get(self._server_name, {})
        tools = server_info.get("tools", [])
        tool_names = [t.get("name") for t in tools]
        
        # Try exact match first, then try with hyphens/underscores swapped
        actual_tool_name = self._tool_name
        if self._tool_name not in tool_names:
            normalized = self._tool_name.replace("_", "-")
            if normalized in tool_names:
                actual_tool_name = normalized
            else:
                normalized = self._tool_name.replace("-", "_")
                if normalized in tool_names:
                    actual_tool_name = normalized
        
        for tool in tools:
            if tool.get("name") == actual_tool_name:
                return {
                    "server": self._server_name,
                    "name": actual_tool_name,
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {})
                }
        
        return {
            "server": self._server_name,
            "name": self._tool_name,
            "error": f"Tool '{self._tool_name}' not found in server '{self._server_name}'",
            "available_tools": tool_names
        }


class _DynamicProxy:
    def __init__(self, server_name, namespace, access_control, ipc_client, manifest):
        self._server_name = server_name
        self._namespace = namespace
        self._access_control = access_control
        self._ipc_client = ipc_client
        self._manifest = manifest

    def __getattr__(self, tool_name):
        return _ToolProxy(self._server_name, tool_name, self._ipc_client, self._manifest)

    def __dir__(self):
        server_info = self._manifest.servers.get(self._server_name, {})
        tools = server_info.get("tools", [])
        return [tool.get("name") for tool in tools]


class _APIProxy:
    def __init__(self, namespace, access_control, ipc_client, manifest):
        self._namespace = namespace
        self._access_control = access_control
        self._ipc_client = ipc_client
        self._manifest = manifest

    def server(self, name):
        can_access, error = self._access_control.can_access(self._namespace, name)
        if not can_access:
            raise PermissionError(error)
        return _DynamicProxy(name, self._namespace, self._access_control, self._ipc_client, self._manifest)

    def call_tool(self, server, tool, args):
        can_access, error = self._access_control.can_access(self._namespace, server)
        if not can_access:
            raise PermissionError(error)
        return self._ipc_client.call(server, tool, args)

    def manifest(self):
        allowed = self._access_control._resolve_allowed_servers(self._namespace)
        return {
            "namespace": self._namespace,
            "allowed_servers": sorted(allowed),
            "servers": {n: _registry.get_server(n) for n in allowed if _registry.get_server(n)},
        }


class _StashProxy:
    def __init__(self, initial_data=None):
        self._data = initial_data or {}
        self._updates = []

    def get(self, key, default=None):
        return self._data.get(key, default)

    def put(self, key, value, ttl_seconds=None):
        self._data[key] = value
        self._updates.append({"op": "put", "key": key, "value": value, "ttl_seconds": ttl_seconds})
        return value

    def has(self, key):
        return key in self._data

    def delete(self, key):
        if key in self._data:
            del self._data[key]
            self._updates.append({"op": "delete", "key": key})
            return True
        return False

    def clear(self):
        self._data.clear()
        self._updates.append({"op": "clear"})

    def keys(self):
        return list(self._data.keys())

    def _get_updates(self):
        return self._updates


def parallel(callables):
    '''Execute multiple tool calls concurrently (synchronous).
    
    Args:
        callables: List of callable functions returning tool results
    
    Returns:
        List of results in same order as input callables
    
    Note:
        max_concurrency is configured in mcproxy.json, not adjustable per-call
    
    Example:
        results = parallel([
            lambda: api.server('s1').tool1(),
            lambda: api.server('s2').tool2(),
        ])
    '''
    import asyncio

    # max_concurrency is injected by executor from mcproxy.json config
    _max_concurrency = _PARALLEL_MAX_CONCURRENCY

    async def _run_async():
        semaphore = asyncio.Semaphore(_max_concurrency)

        async def run_with_limit(coro_func):
            async with semaphore:
                try:
                    result = coro_func()
                    if asyncio.iscoroutine(result):
                        result = await result
                    return {"status": "fulfilled", "result": result}
                except Exception as e:
                    return {"status": "rejected", "error": f"{type(e).__name__}: {str(e)}"}

        tasks = [run_with_limit(c) for c in callables]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final = []
        for r in results:
            if isinstance(r, dict):
                final.append(r)
            elif isinstance(r, Exception):
                final.append({"status": "rejected", "error": f"{type(r).__name__}: {str(r)}"})
            else:
                final.append({"status": "fulfilled", "result": r})

        return final

    return asyncio.run(_run_async())
"""
