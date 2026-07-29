"""aiohttp dashboard server for local VN-300 telemetry."""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import time
from typing import Any
import uuid

from aiohttp import web

from .auth import verify_pin
from .coordinates import local_xy


STATIC_DIR = Path(__file__).with_name("static")
COOKIE_NAME = "vn_operator"
SESSION_SECONDS = 8 * 60 * 60
MAX_TRACK_POINTS = 36_000
SESSION_NAME = re.compile(r"^rfr_vn300_[0-9_]+\.csv$")


@dataclass
class DashboardState:
    log_dir: Path
    latest: dict[str, Any] = field(default_factory=dict)
    trail: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=MAX_TRACK_POINTS)
    )
    origin: tuple[float, float] | None = None
    session_id: str | None = None
    last_source: str | None = None
    segment: int = 0
    clients: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)

    def ingest(self, payload: dict[str, Any]) -> None:
        incoming_session = payload.get("session_id")
        if incoming_session != self.session_id:
            self.session_id = incoming_session
            self.origin = None
            self.trail.clear()
            self.last_source = None
            self.segment = 0

        self.latest = payload
        measurement = payload.get("measurement") or {}
        ins_available = measurement.get("ins_lat") is not None
        if self.origin is None and ins_available:
            self.origin = (
                float(measurement["ins_lat"]),
                float(measurement["ins_lon"]),
            )

        source = None
        latitude = longitude = uncertainty = None
        if self.origin is not None and ins_available:
            source = "ins"
            latitude = measurement["ins_lat"]
            longitude = measurement["ins_lon"]
            uncertainty = measurement.get("ins_pos_u")
        elif self.origin is not None and measurement.get("gnss_lat") is not None:
            source = "gnss"
            latitude = measurement["gnss_lat"]
            longitude = measurement["gnss_lon"]
            axes = [
                measurement.get("gnss_pos_u_n"),
                measurement.get("gnss_pos_u_e"),
            ]
            axes = [float(value) for value in axes if value is not None]
            uncertainty = max(axes) if axes else None

        point = None
        if source is not None and self.origin is not None:
            if self.last_source is not None and source != self.last_source:
                self.segment += 1
            self.last_source = source
            x, y = local_xy(float(latitude), float(longitude), *self.origin)
            point = {
                "x": round(x, 3),
                "y": round(y, 3),
                "source": source,
                "segment": self.segment,
                "uncertainty": uncertainty,
                "timestamp": payload.get("timestamp"),
            }
            self.trail.append(point)

        public_payload = dict(payload)
        public_payload["position"] = point
        public_payload["origin_ready"] = self.origin is not None
        self.latest = public_payload
        for queue in tuple(self.clients):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(public_payload)


class TelemetryProtocol(asyncio.DatagramProtocol):
    def __init__(self, state: DashboardState):
        self.state = state

    def datagram_received(self, data: bytes, _address: Any) -> None:
        try:
            payload = json.loads(data)
            if isinstance(payload, dict):
                self.state.ingest(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return


class AuthManager:
    def __init__(self, auth_file: Path):
        self.record = json.loads(auth_file.read_text(encoding="utf-8"))
        self.sessions: dict[str, float] = {}
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    def authenticated(self, request: web.Request) -> bool:
        token = request.cookies.get(COOKIE_NAME)
        expiry = self.sessions.get(token or "", 0)
        if expiry <= time.monotonic():
            if token:
                self.sessions.pop(token, None)
            return False
        return True

    def login(self, pin: str, address: str) -> str | None:
        now = time.monotonic()
        failures = self.failures[address]
        while failures and now - failures[0] > 60:
            failures.popleft()
        if len(failures) >= 5:
            raise web.HTTPTooManyRequests(text="Too many failed attempts")
        if not verify_pin(pin, self.record):
            failures.append(now)
            return None
        failures.clear()
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + SESSION_SECONDS
        return token


def system_health(log_dir: Path) -> dict[str, Any]:
    memory = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                memory[key] = int(value.strip().split()[0]) * 1024
    except (FileNotFoundError, ValueError):
        pass
    temperature = None
    try:
        temperature = (
            int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        )
    except (FileNotFoundError, ValueError):
        pass
    disk = shutil.disk_usage(log_dir)
    return {
        "memory_total": memory.get("MemTotal"),
        "memory_available": memory.get("MemAvailable"),
        "cpu_temp_c": temperature,
        "disk_total": disk.total,
        "disk_free": disk.free,
    }


def same_origin(request: web.Request) -> None:
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != f"{request.scheme}://{request.host}":
        raise web.HTTPForbidden(text="Cross-origin command rejected")


def require_operator(request: web.Request) -> None:
    same_origin(request)
    if not request.app["auth"].authenticated(request):
        raise web.HTTPUnauthorized(text="Operator authentication required")


def control_request(path: Path, command: str, payload: dict[str, Any]) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    client_path = path.parent / f"dashboard-{os.getpid()}-{request_id[:8]}.sock"
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.bind(str(client_path))
        client.settimeout(15)
        client.sendto(
            json.dumps(
                {"request_id": request_id, "command": command, "payload": payload}
            ).encode(),
            str(path),
        )
        response = json.loads(client.recv(65_535))
        if response.get("request_id") not in {None, request_id}:
            raise RuntimeError("mismatched logger response")
        return response
    finally:
        client.close()
        client_path.unlink(missing_ok=True)


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def status(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            "telemetry": state.latest,
            "health": system_health(state.log_dir),
            "viewers": len(state.clients),
            "operator": request.app["auth"].authenticated(request),
        }
    )


async def track(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            "session_id": state.session_id,
            "origin": state.origin,
            "points": list(state.trail),
        }
    )


async def stream(request: web.Request) -> web.StreamResponse:
    state: DashboardState = request.app["state"]
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
    state.clients.add(queue)
    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
    await response.prepare(request)
    try:
        if state.latest:
            await response.write(
                f"data:{json.dumps(state.latest, separators=(',', ':'))}\n\n".encode()
            )
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=15)
                data = f"data:{json.dumps(payload, separators=(',', ':'))}\n\n"
            except asyncio.TimeoutError:
                data = ":keepalive\n\n"
            await response.write(data.encode())
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        state.clients.discard(queue)
    return response


async def login(request: web.Request) -> web.Response:
    same_origin(request)
    body = await request.json()
    token = request.app["auth"].login(
        str(body.get("pin", "")), request.remote or "unknown"
    )
    if token is None:
        raise web.HTTPUnauthorized(text="Invalid PIN")
    response = web.json_response({"success": True})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="Strict",
    )
    return response


async def logout(request: web.Request) -> web.Response:
    same_origin(request)
    token = request.cookies.get(COOKIE_NAME)
    if token:
        request.app["auth"].sessions.pop(token, None)
    response = web.json_response({"success": True})
    response.del_cookie(COOKIE_NAME)
    return response


async def command(request: web.Request) -> web.Response:
    require_operator(request)
    commands = {
        "start": "start",
        "stop": "stop",
        "new-session": "new_session",
        "marker": "marker",
        "reset-laps": "reset_laps",
        "set-start-finish": "set_start_finish",
        "reconnect": "reconnect",
    }
    command_name = commands.get(request.match_info["command"])
    if command_name is None:
        raise web.HTTPNotFound()
    try:
        payload = await request.json() if request.can_read_body else {}
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON") from None
    lock: asyncio.Lock = request.app["command_lock"]
    async with lock:
        try:
            result = await asyncio.to_thread(
                control_request,
                request.app["control_socket"],
                command_name,
                payload,
            )
        except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError):
            raise web.HTTPServiceUnavailable(text="Logger control is unavailable")
    return web.json_response(result, status=200 if result.get("success") else 409)


async def sessions(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    active = (
        (state.latest or {}).get("filename")
        if (state.latest or {}).get("recording")
        else None
    )
    records = []
    for path in sorted(state.log_dir.glob("rfr_vn300_*.csv"), reverse=True):
        if path.name == active:
            continue
        stat = path.stat()
        records.append(
            {
                "filename": path.name,
                "size": stat.st_size,
                "modified": stat.st_mtime,
            }
        )
    return web.json_response({"sessions": records[:100]})


async def download(request: web.Request) -> web.StreamResponse:
    state: DashboardState = request.app["state"]
    filename = request.match_info["filename"]
    if not SESSION_NAME.fullmatch(filename):
        raise web.HTTPNotFound()
    if (
        (state.latest or {}).get("recording")
        and filename == (state.latest or {}).get("filename")
    ):
        raise web.HTTPConflict(text="Active session must be stopped or rotated")
    path = state.log_dir / filename
    if not path.is_file() or path.parent != state.log_dir:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def create_app(args: argparse.Namespace) -> web.Application:
    state = DashboardState(args.log_dir)
    app = web.Application(client_max_size=16 * 1024)
    app["state"] = state
    app["auth"] = AuthManager(args.auth_file)
    app["control_socket"] = args.control_socket
    app["command_lock"] = asyncio.Lock()

    async def start_telemetry(application: web.Application) -> None:
        args.telemetry_socket.parent.mkdir(parents=True, exist_ok=True)
        args.telemetry_socket.unlink(missing_ok=True)
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: TelemetryProtocol(state),
            local_addr=str(args.telemetry_socket),
            family=socket.AF_UNIX,
        )
        os.chmod(args.telemetry_socket, 0o660)
        application["telemetry_transport"] = transport

    async def stop_telemetry(application: web.Application) -> None:
        application["telemetry_transport"].close()
        args.telemetry_socket.unlink(missing_ok=True)

    app.on_startup.append(start_telemetry)
    app.on_cleanup.append(stop_telemetry)
    app.router.add_get("/", index)
    app.router.add_static("/static", STATIC_DIR)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/track", track)
    app.router.add_get("/api/stream", stream)
    app.router.add_post("/api/operator/login", login)
    app.router.add_post("/api/operator/logout", logout)
    app.router.add_post("/api/commands/{command}", command)
    app.router.add_get("/api/sessions", sessions)
    app.router.add_get("/api/sessions/{filename}", download)
    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RFR VectorNav live dashboard")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log-dir", type=Path, default=Path("/var/lib/vectornav/logs"))
    parser.add_argument(
        "--auth-file", type=Path, default=Path("/var/lib/vectornav/dashboard-auth.json")
    )
    parser.add_argument(
        "--control-socket", type=Path, default=Path("/run/vectornav/logger-control.sock")
    )
    parser.add_argument(
        "--telemetry-socket",
        type=Path,
        default=Path("/run/vectornav/dashboard-telemetry.sock"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    web.run_app(create_app(args), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
