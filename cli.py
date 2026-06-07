"""CLI tool for managing MCP server systemd services."""

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

ADAPTER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapter.py")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcproxy.json")

SYSTEM_UNIT_DIR = "/etc/systemd/system"
USER_UNIT_DIR = os.path.expanduser("~/.config/systemd/user")


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


def save_config(config: dict, path: str = CONFIG_PATH) -> None:
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def allocate_port(config: dict, base_port: int = 12020) -> int:
    from urllib.parse import urlparse

    used = set()
    for server in config.get("servers", []):
        url = server.get("url", "")
        if url and isinstance(url, str):
            try:
                parsed = urlparse(url)
                if parsed.port is not None:
                    used.add(parsed.port)
            except Exception:
                pass
    port = base_port
    while port in used:
        port += 1
    return port


def unit_dir(user: bool) -> str:
    return USER_UNIT_DIR if user else SYSTEM_UNIT_DIR


def unit_path(name: str, user: bool) -> str:
    return os.path.join(unit_dir(user), f"mcp-{name}.service")


def systemctl(
    args: list[str], user: bool, check: bool = True
) -> subprocess.CompletedProcess:
    cmd = ["systemctl"]
    if user:
        cmd.append("--user")
    cmd.extend(args)
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def generate_unit(
    name: str,
    command_parts: list[str],
    port: int,
    user: bool,
    env: dict[str, str] | None = None,
) -> str:
    target = "default.target" if user else "multi-user.target"
    python_path = sys.executable
    exec_args = " ".join(command_parts)

    env_lines = ""
    if env:
        env_lines = "\n".join(f"Environment={k}={v}" for k, v in env.items())
        env_lines += "\n"

    return (
        f"[Unit]\n"
        f"Description=MCP Server: {name}\n"
        f"After=network.target\n"
        f"\n"
        f"[Service]\n"
        f"Type=simple\n"
        f"ExecStart={python_path} {ADAPTER_PATH} --port {port} --host 127.0.0.1 -- {exec_args}\n"
        f"Restart=on-failure\n"
        f"RestartSec=5\n"
        f"{env_lines}"
        f"\n"
        f"[Install]\n"
        f"WantedBy={target}\n"
    )


def cmd_add(args: argparse.Namespace) -> None:
    command_parts = shlex.split(args.command)
    config = load_config()

    if any(s["name"] == args.name for s in config.get("servers", [])):
        print(f"Error: server '{args.name}' already exists in config", file=sys.stderr)
        sys.exit(1)

    port = args.port if args.port else allocate_port(config)

    env = None
    if args.env:
        env = {}
        for pair in args.env:
            if "=" not in pair:
                print(
                    f"Error: invalid env format '{pair}', expected KEY=VALUE",
                    file=sys.stderr,
                )
                sys.exit(1)
            k, v = pair.split("=", 1)
            env[k] = v

    unit = generate_unit(args.name, command_parts, port, args.user, env)
    udir = unit_dir(args.user)
    os.makedirs(udir, exist_ok=True)

    upath = unit_path(args.name, args.user)
    with open(upath, "w") as f:
        f.write(unit)

    systemctl(["daemon-reload"], args.user)
    systemctl(["enable", f"mcp-{args.name}.service"], args.user)

    config.setdefault("servers", []).append(
        {
            "name": args.name,
            "url": f"http://localhost:{port}/mcp",
            "timeout": 60,
            "enabled": True,
        }
    )
    save_config(config)

    print(f"Added '{args.name}' → http://localhost:{port}/mcp")
    print(f"Unit: {upath}")


def cmd_remove(args: argparse.Namespace) -> None:
    upath = unit_path(args.name, args.user)

    try:
        systemctl(["stop", f"mcp-{args.name}.service"], args.user, check=False)
    except Exception:
        pass

    try:
        systemctl(["disable", f"mcp-{args.name}.service"], args.user, check=False)
    except Exception:
        pass

    if os.path.exists(upath):
        os.remove(upath)

    systemctl(["daemon-reload"], args.user)

    config = load_config()
    config["servers"] = [s for s in config.get("servers", []) if s["name"] != args.name]
    save_config(config)

    print(f"Removed '{args.name}'")


def cmd_list(args: argparse.Namespace) -> None:
    udir = unit_dir(args.user)
    config = load_config()
    url_map = {s["name"]: s.get("url", "") for s in config.get("servers", [])}

    if not os.path.isdir(udir):
        return

    rows = []
    for fname in sorted(os.listdir(udir)):
        if not fname.startswith("mcp-") or not fname.endswith(".service"):
            continue
        name = fname[4:-8]
        upath = os.path.join(udir, fname)

        with open(upath) as f:
            content = f.read()

        command = ""
        for line in content.splitlines():
            if line.startswith("ExecStart="):
                after_marker = line.split(" -- ", 1)
                if len(after_marker) == 2:
                    command = after_marker[1].strip()
                break

        try:
            result = systemctl(
                ["is-active", f"mcp-{name}.service"], args.user, check=False
            )
            status = result.stdout.strip()
        except Exception:
            status = "unknown"

        url = url_map.get(name, "")
        rows.append((name, status, url, command))

    if not rows:
        print("No MCP services found.")
        return

    name_w = max(len("NAME"), *(len(r[0]) for r in rows))
    status_w = max(len("STATUS"), *(len(r[1]) for r in rows))
    url_w = max(len("URL"), *(len(r[2]) for r in rows))

    header = f"{'NAME':<{name_w}}  {'STATUS':<{status_w}}  {'URL':<{url_w}}  COMMAND"
    print(header)
    print("-" * len(header))
    for name, status, url, command in rows:
        print(f"{name:<{name_w}}  {status:<{status_w}}  {url:<{url_w}}  {command}")


def cmd_service(action: str, name: str, user: bool) -> None:
    systemctl([f"{action}", f"mcp-{name}.service"], user)
    print(f"{action.capitalize()}ed 'mcp-{name}.service'")


def cmd_migrate(args: argparse.Namespace) -> None:
    config = load_config()
    servers = config.get("servers", [])
    port = args.port or 12020
    migrated = 0
    skipped = 0
    commands = []

    for server in servers:
        if "url" in server:
            skipped += 1
            continue

        if "command" not in server:
            skipped += 1
            continue

        name = server["name"]
        command_parts = [server["command"]] + server.get("args", [])
        env = server.get("env")

        unit = generate_unit(name, command_parts, port, args.user, env)
        udir = unit_dir(args.user)
        os.makedirs(udir, exist_ok=True)
        upath = unit_path(name, args.user)
        with open(upath, "w") as f:
            f.write(unit)

        server["url"] = f"http://localhost:{port}/mcp"
        server.pop("command", None)
        server.pop("args", None)
        server.pop("env", None)
        server.pop("type", None)
        migrated += 1
        commands.append(
            f"systemctl {'--user ' if args.user else ''}enable --now mcp-{name}.service"
        )
        port += 1

    if migrated == 0:
        print("No stdio servers to migrate.")
        return

    save_config(config)
    systemctl(["daemon-reload"], args.user)

    print(f"Migrated {migrated} servers ({skipped} skipped)")
    print(f"\nEnable and start services:")
    for cmd in commands:
        print(f"  {cmd}")


def cmd_test(args: argparse.Namespace) -> None:
    """Test a backend server's connectivity and tool response."""
    import requests

    config = load_config()
    server_name = args.name

    # Find the server
    server_config = None
    for s in config.get("servers", []):
        if s["name"] == server_name:
            server_config = s
            break

    if not server_config:
        print(f"Error: server '{server_name}' not found in config", file=sys.stderr)
        servers = [s["name"] for s in config.get("servers", [])]
        print(f"Available servers: {', '.join(servers)}", file=sys.stderr)
        sys.exit(1)

    url = server_config.get("url")
    if not url:
        print(
            f"Error: server '{server_name}' has no URL (stdio server)",
            file=sys.stderr,
        )
        sys.exit(1)

    action = args.action or "tools/list"
    timeout = args.timeout or server_config.get("timeout", 60)

    print(f"Testing server '{server_name}' at {url}")
    print(f"Action: {action} | Timeout: {timeout}s")
    print("-" * 50)

    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
    }
    custom_headers = server_config.get("headers", {})
    headers.update(custom_headers)

    # Step 1: Initialize
    print("\n[1] Initialize...")
    init_payload = {
        "jsonrpc": "2.0",
        "id": "init",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcproxy-test", "version": "1.0.0"},
        },
    }
    session_id = None

    try:
        resp = requests.post(url, json=init_payload, headers=headers, timeout=timeout)
        session_id = resp.headers.get("mcp-session-id")
        resp.raise_for_status()

        ct = resp.headers.get("content-type", "")
        init_result = None
        if "application/json" in ct:
            init_result = resp.json()
        else:
            for line in resp.iter_lines():
                if line and line.decode().strip().startswith("data:"):
                    import json as json_mod

                    init_result = json_mod.loads(line.decode().strip()[5:].strip())
                    break

        if not init_result:
            print("  ❌ No response")
            sys.exit(1)
        if "error" in init_result:
            print(f"  ❌ Error: {init_result['error']}")
            sys.exit(1)

        server_info = init_result.get("result", {}).get("serverInfo", {})
        print(f"  ✅ Connected: {server_info.get('name', '?')} v{server_info.get('version', '?')}")

    except Exception as e:
        print(f"  ❌ Connection failed: {e}")
        sys.exit(1)

    # Step 2: Perform action
    print(f"\n[2] {action}...")
    action_headers = dict(headers)
    if session_id:
        action_headers["mcp-session-id"] = session_id

    action_payload = {"jsonrpc": "2.0", "id": "action", "method": action, "params": {}}

    try:
        resp = requests.post(
            url, json=action_payload, headers=action_headers, timeout=timeout
        )
        ct = resp.headers.get("content-type", "")
        action_result = None
        if "application/json" in ct:
            action_result = resp.json()
        else:
            for line in resp.iter_lines():
                if line and line.decode().strip().startswith("data:"):
                    import json as json_mod

                    action_result = json_mod.loads(line.decode().strip()[5:].strip())
                    break

        if not action_result:
            print("  ❌ No response")
            sys.exit(1)
        if "error" in action_result:
            print(f"  ❌ Error: {action_result['error']}")
            sys.exit(1)

        if action == "tools/list":
            tools = action_result.get("result", {}).get("tools", [])
            print(f"  ✅ {len(tools)} tools discovered:")
            for tool in tools:
                desc = (tool.get("description") or "").split("\n")[0][:60]
                print(f"     - {tool['name']}: {desc}")
        else:
            print(f"  ✅ Result:")
            print(json.dumps(action_result.get("result", {}), indent=2)[:500])

    except Exception as e:
        print(f"  ❌ Failed: {e}")
        sys.exit(1)

    print(f"\n{'=' * 50}")
    print("All checks passed ✅")


def cmd_validate(args: argparse.Namespace) -> None:
    """Validate connectivity to all enabled HTTP backends."""
    import requests

    config = load_config()
    servers = [s for s in config.get("servers", []) if s.get("enabled", True)]

    http_servers = [s for s in servers if "url" in s]
    stdio_servers = [s for s in servers if "command" in s]

    print(f"Validating {len(http_servers)} HTTP backends...")
    if stdio_servers:
        print(
            f"(Skipping {len(stdio_servers)} stdio servers: "
            f"{', '.join(s['name'] for s in stdio_servers)})"
        )
    print("-" * 50)

    failures = []
    for server in http_servers:
        name = server["name"]
        url = server["url"]
        timeout = server.get("timeout", 60)

        try:
            hdrs = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json",
            }
            hdrs.update(server.get("headers", {}))

            payload = {
                "jsonrpc": "2.0",
                "id": "health",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "mcproxy-validate",
                        "version": "1.0.0",
                    },
                },
            }

            resp = requests.post(url, json=payload, headers=hdrs, timeout=timeout)
            ct = resp.headers.get("content-type", "")
            result = None
            if "application/json" in ct:
                result = resp.json()
            else:
                for line in resp.iter_lines():
                    if line and line.decode().strip().startswith("data:"):
                        import json as json_mod

                        result = json_mod.loads(line.decode().strip()[5:].strip())
                        break

            if result and "error" not in result:
                server_info = result.get("result", {}).get("serverInfo", {})
                sname = server_info.get("name", "?")
                ver = server_info.get("version", "?")
                print(f"  ✅ {name:<25} {url:<45} ({sname} v{ver})")
            else:
                err = (result or {}).get("error", "no response")
                print(f"  ❌ {name:<25} {url:<45} error: {err}")
                failures.append(name)

        except Exception as e:
            print(f"  ❌ {name:<25} {url:<45} {type(e).__name__}: {e}")
            failures.append(name)

    print("-" * 50)
    if failures:
        print(f"❌ {len(failures)} server(s) failed: {', '.join(failures)}")
        sys.exit(1)
    else:
        print(f"✅ All {len(http_servers)} backends healthy")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mcproxy",
        description="Manage MCP server systemd services",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_add = subparsers.add_parser("add", help="Add a new MCP server service")
    p_add.add_argument("name", help="Server name")
    p_add.add_argument(
        "--command", required=True, help='Command string (e.g. "npx -y wikipedia-mcp")'
    )
    p_add.add_argument(
        "--port", type=int, default=None, help="Port number (auto-allocated if omitted)"
    )
    p_add.add_argument(
        "--env",
        action="append",
        default=None,
        help="Environment variable KEY=VALUE (repeatable)",
    )
    p_add.add_argument("--user", action="store_true", help="Install as user service")
    p_add.set_defaults(func=cmd_add)

    p_remove = subparsers.add_parser("remove", help="Remove an MCP server service")
    p_remove.add_argument("name", help="Server name")
    p_remove.add_argument("--user", action="store_true", help="User service")
    p_remove.set_defaults(func=cmd_remove)

    p_list = subparsers.add_parser("list", help="List MCP server services")
    p_list.add_argument("--user", action="store_true", help="List user services")
    p_list.set_defaults(func=cmd_list)

    p_migrate = subparsers.add_parser(
        "migrate", help="Migrate stdio servers to HTTP adapter services"
    )
    p_migrate.add_argument(
        "--port",
        type=int,
        default=None,
        help="Starting port for auto-allocation (default: 12020)",
    )
    p_migrate.add_argument(
        "--user", action="store_true", help="Install as user services"
    )
    p_migrate.set_defaults(func=cmd_migrate)

    for action in ("start", "stop", "restart"):
        p = subparsers.add_parser(
            action, help=f"{action.capitalize()} an MCP server service"
        )
        p.add_argument("name", help="Server name")
        p.add_argument("--user", action="store_true", help="User service")
        p.set_defaults(func=lambda a, act=action: cmd_service(act, a.name, a.user))

    p_test = subparsers.add_parser("test", help="Test a backend server's connectivity")
    p_test.add_argument("name", help="Server name to test")
    p_test.add_argument(
        "--action",
        default="tools/list",
        help="MCP action to perform (default: tools/list)",
    )
    p_test.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Request timeout in seconds (default: from config)",
    )
    p_test.set_defaults(func=cmd_test)

    p_validate = subparsers.add_parser(
        "validate", help="Validate connectivity to all HTTP backends"
    )
    p_validate.set_defaults(func=cmd_validate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
