"""Installation identity, capability and browser credentials for local assistants."""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app import config

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
COOKIE = "tripfinder_agent_ui"
_sessions: dict[str, tuple[str, float]] = {}
_registered_instance: str | None = None


def state_dir() -> Path:
    return config.DATA_DIR


def _private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("unsafe_private_path")
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(text)
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def instance_id() -> str:
    path = state_dir() / "instance-id"
    if path.is_symlink():
        raise ValueError("unsafe_instance_path")
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("invalid_instance_id")
        return value
    value = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(value)
        path.chmod(0o600)
    except FileExistsError:
        return instance_id()
    return value


def token_path() -> Path:
    return state_dir() / "agent" / "mcp-token.txt"


def capability(*, create: bool = False) -> str | None:
    path = token_path()
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("unsafe_token_path")
    if not path.exists():
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(48))
            path.chmod(0o600)
        except FileExistsError:
            pass
    value = path.read_text(encoding="utf-8").strip()
    if not 32 <= len(value) <= 256 or any(c.isspace() for c in value):
        raise ValueError("invalid_token_file")
    return value


def valid_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in LOCAL_HOSTS
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("local_origin_required")
    return value.rstrip("/")


def register_runtime(base_url: str, pid: int | None = None) -> dict:
    global _registered_instance
    from app.version import VERSION
    identity = instance_id()
    descriptor = {"instance_id": identity, "base_url": valid_origin(base_url),
                  "pid": pid or os.getpid(), "version": VERSION,
                  "token_path": str(token_path())}
    _private_write(state_dir() / "runtime.json", json.dumps(descriptor))
    _registered_instance = identity
    return descriptor


def unregister_runtime() -> None:
    path = state_dir() / "runtime.json"
    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        if descriptor.get("instance_id") == _registered_instance and descriptor.get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass
    _sessions.clear()


def require_local(request: Request, *, origin_required: bool = False) -> None:
    if request.url.hostname not in LOCAL_HOSTS:
        raise HTTPException(403, "local_host_required")
    origin = request.headers.get("origin")
    if origin:
        try:
            if valid_origin(origin) != f"{request.url.scheme}://{request.url.netloc}":
                raise ValueError
        except ValueError:
            raise HTTPException(403, "same_origin_required") from None
    elif origin_required:
        raise HTTPException(403, "origin_required")


def new_ui_session() -> tuple[str, str]:
    now = time.time()
    for key in list(_sessions):
        if _sessions[key][1] <= now:
            _sessions.pop(key, None)
    if len(_sessions) >= 100:
        raise HTTPException(429, "too_many_ui_sessions")
    session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    _sessions[session] = (csrf, now + 12 * 3600)
    return session, csrf


def require_ui(request: Request) -> None:
    mutating = request.method not in {"GET", "HEAD"}
    require_local(request, origin_required=mutating)
    session = _sessions.get(request.cookies.get(COOKIE, ""))
    csrf = request.headers.get("x-tripfinder-csrf", "")
    if (not session or session[1] <= time.time()
            or (mutating and (not csrf or not secrets.compare_digest(session[0], csrf)))):
        raise HTTPException(403, "ui_session_required")


def require_mcp(request: Request) -> None:
    require_local(request)
    header = request.headers.get("authorization", "")
    bearer = header[7:] if header.startswith("Bearer ") else ""
    try:
        expected = capability()
    except ValueError:
        expected = None
    if not expected or not bearer or not secrets.compare_digest(expected, bearer):
        raise HTTPException(401, "agent_token_required", headers={"WWW-Authenticate": "Bearer"})
    claimed = request.headers.get("x-tripfinder-instance")
    if claimed and not secrets.compare_digest(instance_id(), claimed):
        raise HTTPException(409, "installation_mismatch")


def setup(base_url: str) -> dict:
    capability(create=True)
    descriptor = register_runtime(base_url)
    runtime_file = str(state_dir() / "runtime.json")
    if getattr(sys, "frozen", False):
        from app.install_layout import installation_root, is_organized
        physical_root = installation_root()
        organized = is_organized(physical_root)
        command = str(physical_root / ("TripFinder.exe" if organized else "TripFinderMCP.exe"))
        args = ["--mcp-stdio" if organized else "--stdio",
                "--token-file", str(token_path()), "--runtime-file", runtime_file]
    else:
        command = sys.executable
        args = [str(Path(__file__).with_name("mcp_server.py")), "--stdio",
                "--token-file", str(token_path()), "--runtime-file", runtime_file]
    args.extend(["--instance-id", descriptor["instance_id"]])
    toml = ("[mcp_servers.tripfinder]\ncommand = " + json.dumps(command)
            + "\nargs = " + json.dumps(args) + "\n")
    desktop = {"mcpServers": {"tripfinder": {"command": command, "args": args}}}
    hermes = ("mcp_servers:\n  tripfinder:\n    command: " + json.dumps(command)
              + "\n    args: " + json.dumps(args) + "\n")
    return {"endpoint": descriptor["base_url"] + "/mcp/", "token_file": str(token_path()),
            "configs": {"codex": toml, "claude_desktop": desktop,
                        "claude_code": desktop, "hermes": hermes}}
