#!/usr/bin/env python3
"""Personal Edge TTS CLI: text → ordered MP3 chunks (~1.5 min each)."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hmac
import html
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import edge_tts
from aiohttp import web

# English speech is ~140–160 wpm; ~5 chars/word + spaces ≈ 750–900 chars/min.
# 1.5 minutes ≈ 1100–1350 chars. Default 1400 leaves a little headroom.
DEFAULT_CHUNK_CHARS = 1400
DEFAULT_VOICE = "en-US-EmmaMultilingualNeural"
DEFAULT_LISTEN = "127.0.0.1"
DEFAULT_HOSTNAME = "localhost"

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
WHITESPACE = re.compile(r"\s+")

ALLOWED_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".mp3": "audio/mpeg",
}

SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}

# Crockford base32 (no I, L, O, U) — 8 chars ≈ 40 bits of entropy.
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PAIR_TTL_SEC = 300
PAIR_MAX_PENDING = 16


def split_text(text: str, max_chars: int) -> list[str]:
    text = WHITESPACE.sub(" ", text).strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    for sentence in SENTENCE_SPLIT.split(text):
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        # Hard-wrap oversized sentences on word boundaries.
        words = sentence.split(" ")
        buf: list[str] = []
        size = 0
        for word in words:
            add = len(word) + (1 if buf else 0)
            if buf and size + add > max_chars:
                pieces.append(" ".join(buf))
                buf = [word]
                size = len(word)
            else:
                buf.append(word)
                size += add
        if buf:
            pieces.append(" ".join(buf))

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for piece in pieces:
        add = len(piece) + (1 if current else 0)
        if current and size + add > max_chars:
            chunks.append(" ".join(current))
            current = [piece]
            size = len(piece)
        else:
            current.append(piece)
            size += add
    if current:
        chunks.append(" ".join(current))
    return chunks


def read_input(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.text is not None:
        return args.text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("Provide --text, --file, or pipe text on stdin.")


def unique_stem(out_dir: Path, prefix: str) -> str:
    """Pick a stem that does not collide with existing HTML/MP3s in out_dir."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 0
    while True:
        stem = f"{prefix}_{stamp}" if n == 0 else f"{prefix}_{stamp}_{n}"
        html_path = out_dir / f"{stem}.html"
        if not html_path.exists() and not any(out_dir.glob(f"{stem}_*.mp3")):
            return stem
        n += 1


def write_player_html(
    text: str,
    mp3_paths: list[Path],
    out_dir: Path,
    stem: str,
    expected: int,
) -> Path:
    items = []
    for path in mp3_paths:
        name = html.escape(path.name)
        items.append(
            f"<li><code>{name}</code>"
            f'<audio src="{name}" preload="metadata" controls></audio></li>'
        )
    body = "\n".join(items)
    ready = len(mp3_paths)
    pending = ready < expected
    status = (
        f"<p><small>Generating… {ready}/{expected}</small></p>"
        if pending
        else f"<p><small>{ready} clip(s)</small></p>"
    )
    refresh = '<meta http-equiv="refresh" content="2">' if pending else ""
    escaped = html.escape(text)
    title = html.escape(stem)
    doc = f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}
<title>{title}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
<main class="container">
<h1>{title}</h1>
{status}
<p><button type="button" id="play-all">Play all</button></p>
<ol id="clips">{body}</ol>
<article><pre style="white-space:pre-wrap;margin:0">{escaped}</pre></article>
</main>
<script>
const clips=[...document.querySelectorAll("#clips audio")];
let i=-1,stop=()=>{{clips.forEach(a=>{{a.pause();a.currentTime=0}})}};
const next=()=>{{stop();i++;if(i>=clips.length){{i=-1;return}}clips[i].play()}};
clips.forEach(a=>a.addEventListener("ended",next));
document.getElementById("play-all").onclick=()=>{{i=-1;next()}};
</script>
"""
    path = out_dir / f"{stem}.html"
    path.write_text(doc, encoding="utf-8")
    return path.resolve()


async def synthesize(
    chunks: list[str],
    out_dir: Path,
    voice: str,
    stem: str,
    text: str,
) -> list[Path]:
    paths: list[Path] = []
    expected = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        path = out_dir / f"{stem}_{i:03d}.mp3"
        await edge_tts.Communicate(chunk, voice).save(str(path))
        resolved = path.resolve()
        paths.append(resolved)
        print(resolved, flush=True)
        write_player_html(text, paths, out_dir, stem, expected)
    return paths


def token_ok(candidate: str | None, token: str) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode(), token.encode())


def state_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        path = Path(runtime) / "python-tts"
    else:
        path = Path.home() / ".local" / "state" / "python-tts"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def pair_socket_path(port: int) -> Path:
    return state_dir() / f"pair-{port}.sock"


def server_state_path(port: int) -> Path:
    return state_dir() / f"server-{port}.json"


def write_json_private(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def prune_server_state(port: int | None = None, state_path: Path | None = None) -> None:
    """Remove server state json and pairing socket for a port."""
    if state_path is None and port is not None:
        state_path = server_state_path(port)
    if state_path is not None:
        try:
            if port is None:
                try:
                    info = json.loads(state_path.read_text(encoding="utf-8"))
                    port = info.get("port") if isinstance(info.get("port"), int) else None
                except (OSError, json.JSONDecodeError):
                    port = None
            state_path.unlink(missing_ok=True)
        except OSError:
            pass
    if isinstance(port, int):
        try:
            pair_socket_path(port).unlink(missing_ok=True)
        except OSError:
            pass


def ipc_call_sync(sock_path: Path, payload: dict, timeout: float = 1.0) -> dict | None:
    """Blocking Unix IPC; None if the socket is dead/stale."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(sock_path))
            sock.sendall((json.dumps(payload) + "\n").encode())
            chunks: list[bytes] = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
                if b"\n" in data:
                    break
            raw = b"".join(chunks)
            if not raw:
                return None
            return json.loads(raw.decode().splitlines()[0])
    except (OSError, TimeoutError, json.JSONDecodeError, IndexError):
        return None


@contextmanager
def spawn_lock():
    """Exclusive lock around find-or-spawn so concurrent --server can't double-start."""
    path = state_dir() / "spawn.lock"
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def find_live_server(
    out_dir: Path, listen: str, port: int = 0
) -> dict | None:
    """Return state for a live daemon serving this out_dir+listen (+port if set)."""
    want = out_dir.resolve()
    for path in sorted(state_dir().glob("server-*.json")):
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        srv_port = info.get("port")
        pid = info.get("pid")
        if not isinstance(srv_port, int) or not isinstance(pid, int) or not info.get("token"):
            prune_server_state(state_path=path)
            continue
        if port and srv_port != port:
            continue
        try:
            same_dir = Path(str(info.get("out_dir", ""))).resolve() == want
        except OSError:
            same_dir = False
        if not same_dir or info.get("listen") != listen:
            continue
        ping = ipc_call_sync(pair_socket_path(srv_port), {"op": "ping"})
        if not ping or not ping.get("ok") or ping.get("pid") != pid:
            prune_server_state(srv_port, path)
            continue
        return info
    return None


def spawn_background_server(
    out_dir: Path,
    listen: str,
    hostname: str,
    port: int,
    token: str,
) -> dict:
    """Start a detached serve process; return ready info (port, pid, …)."""
    ready_path = state_dir() / f"ready-{os.getpid()}-{secrets.token_hex(4)}.json"
    if ready_path.exists():
        ready_path.unlink()

    log_path = state_dir() / f"daemon-{os.getpid()}.log"
    env = os.environ.copy()
    env["PYTHON_TTS_TOKEN"] = token
    env["PYTHON_TTS_READY"] = str(ready_path)

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--daemon-serve",
        "--out-dir",
        str(out_dir),
        "--listen",
        listen,
        "--hostname",
        hostname,
        "--port",
        str(port),
    ]
    log_f = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — kept for daemon lifetime
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        log_f.close()
        raise

    deadline = time.time() + 20
    while time.time() < deadline:
        if ready_path.exists():
            try:
                info = json.loads(ready_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.05)
                continue
            try:
                ready_path.unlink()
            except OSError:
                pass
            info["log"] = str(log_path)
            log_f.close()
            return info
        if proc.poll() is not None:
            log_f.close()
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8")[-2000:]
            except OSError:
                pass
            raise SystemExit(
                f"background server exited early (code {proc.returncode})."
                + (f"\n{tail}" if tail else "")
            )
        time.sleep(0.05)

    log_f.close()
    try:
        proc.terminate()
    except OSError:
        pass
    raise SystemExit("background server failed to become ready in time")


def ensure_background_server(
    out_dir: Path,
    listen: str,
    hostname: str,
    port: int,
    *,
    force_new: bool = False,
) -> tuple[dict, bool]:
    """Return (server info, reused). Reuses a live server for out_dir+listen unless force_new."""
    with spawn_lock():
        if not force_new:
            existing = find_live_server(out_dir, listen, port)
            if existing is not None:
                return existing, True
        token = secrets.token_urlsafe(32)
        info = spawn_background_server(
            out_dir.resolve(), listen, hostname, port, token
        )
        return info, False


def make_pair_code() -> tuple[str, str]:
    """Return (normalized 8-char code, display form XXXX-XXXX)."""
    n = int.from_bytes(secrets.token_bytes(5), "big")
    chars: list[str] = []
    for _ in range(8):
        chars.append(CROCKFORD[n & 31])
        n >>= 5
    norm = "".join(reversed(chars))
    return norm, f"{norm[:4]}-{norm[4:]}"


def normalize_pair_code(code: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]", "", code).upper()
    # Common Crockford confusions.
    table = str.maketrans({"I": "1", "L": "1", "O": "0"})
    return cleaned.translate(table)


def codes_match(a: str, b: str) -> bool:
    na, nb = normalize_pair_code(a), normalize_pair_code(b)
    if len(na) != 8 or len(nb) != 8:
        return False
    return hmac.compare_digest(na.encode(), nb.encode())


def resolve_served_path(root: Path, url_path: str) -> Path | None:
    """Jail path under root; only regular .html / .mp3 files."""
    rel = urlsplit(url_path).path.lstrip("/")
    if not rel or "\0" in rel:
        return None
    parts = Path(rel).parts
    if any(part.startswith(".") for part in parts):
        return None
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root):
        return None
    if not candidate.is_file() or candidate.suffix not in ALLOWED_TYPES:
        return None
    return candidate


def safe_redirect_location(root: Path, url_path: str) -> str | None:
    """Build a same-origin relative Location, or None if the path is unsafe."""
    raw = urlsplit(url_path).path or ""
    if not raw.startswith("/") or raw.startswith("//"):
        return None
    if "\\" in raw or "\0" in raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        return None
    resolved = resolve_served_path(root, raw)
    if resolved is None:
        return None
    location = "/" + resolved.relative_to(root).as_posix()
    if not location.startswith("/") or location.startswith("//"):
        return None
    return location


def is_html_path(url_path: str) -> bool:
    path = urlsplit(url_path).path or ""
    return path.endswith(".html")


@dataclass
class PendingPair:
    pending_id: str
    code_norm: str
    code_display: str
    remote: str
    user_agent: str
    path: str
    created: float = field(default_factory=time.time)
    approved: bool = False


class PairingHub:
    """In-memory pending pairings + Unix-socket approve channel."""

    def __init__(self, token: str, port: int, root: Path) -> None:
        self.token = token
        self.port = port
        self.root = root.resolve()
        self.cookie_name = f"tts_{port}"
        self.pair_cookie_name = f"tts_pair_{port}"
        self._by_id: dict[str, PendingPair] = {}
        self._lock = asyncio.Lock()
        self.stop_event = asyncio.Event()

    def purge_expired(self) -> None:
        now = time.time()
        expired = [
            pid
            for pid, entry in self._by_id.items()
            if now - entry.created > PAIR_TTL_SEC
        ]
        for pid in expired:
            del self._by_id[pid]

    async def get_or_create_pending(
        self,
        pending_id: str | None,
        remote: str,
        user_agent: str,
        path: str,
    ) -> PendingPair:
        async with self._lock:
            self.purge_expired()
            if pending_id and pending_id in self._by_id:
                entry = self._by_id[pending_id]
                entry.path = path
                return entry

            while len(self._by_id) >= PAIR_MAX_PENDING:
                oldest = min(self._by_id.values(), key=lambda e: e.created)
                del self._by_id[oldest.pending_id]

            code_norm, code_display = make_pair_code()
            entry = PendingPair(
                pending_id=secrets.token_urlsafe(32),
                code_norm=code_norm,
                code_display=code_display,
                remote=remote or "-",
                user_agent=(user_agent or "-")[:200],
                path=path,
            )
            self._by_id[entry.pending_id] = entry
            return entry

    def _find_by_code(self, code: str) -> PendingPair | None:
        self.purge_expired()
        for entry in self._by_id.values():
            if codes_match(code, entry.code_norm):
                return entry
        return None

    async def lookup(self, code: str) -> dict:
        async with self._lock:
            entry = self._find_by_code(code)
            if entry is None:
                return {"ok": False, "error": "unknown or expired code"}
            return {
                "ok": True,
                "port": self.port,
                "remote": entry.remote,
                "user_agent": entry.user_agent,
                "age_sec": int(time.time() - entry.created),
                "path": entry.path,
                "approved": entry.approved,
            }

    async def approve(self, code: str) -> dict:
        async with self._lock:
            entry = self._find_by_code(code)
            if entry is None:
                return {"ok": False, "error": "unknown or expired code"}
            entry.approved = True
            return {"ok": True, "port": self.port, "path": entry.path}

    async def take_approved(self, pending_id: str | None) -> PendingPair | None:
        if not pending_id:
            return None
        async with self._lock:
            self.purge_expired()
            entry = self._by_id.get(pending_id)
            if entry is None or not entry.approved:
                return None
            del self._by_id[pending_id]
            return entry

    async def handle_ipc(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            if not raw:
                return
            op = None
            try:
                req = json.loads(raw.decode())
            except json.JSONDecodeError:
                resp = {"ok": False, "error": "invalid json"}
            else:
                op = req.get("op")
                code = str(req.get("code", ""))
                if op == "lookup":
                    resp = await self.lookup(code)
                elif op == "approve":
                    resp = await self.approve(code)
                elif op == "ping":
                    resp = {"ok": True, "pid": os.getpid(), "port": self.port}
                elif op == "shutdown":
                    resp = {"ok": True, "pid": os.getpid(), "port": self.port}
                else:
                    resp = {"ok": False, "error": "unknown op"}
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
            if op == "shutdown":
                self.stop_event.set()
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


def pair_forbidden_page(port: int, code_display: str) -> str:
    code = html.escape(code_display)
    cmd = html.escape(f"tts --pair {code_display}")
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pairing required</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
<main class="container">
<h1>Not authorized</h1>
<p>This browser has no valid session cookie (missing or outdated).</p>
<p>On the machine running the TTS server (port {port}), run:</p>
<pre><code>{cmd}</code></pre>
<p>Pairing code: <strong><code>{code}</code></strong></p>
<p>Then reload this page.</p>
</main>
"""


def make_server_app(hub: PairingHub) -> web.Application:
    root = hub.root
    token = hub.token

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        q_token = request.rel_url.query.get("token")
        from_query = token_ok(q_token, token)
        from_cookie = token_ok(request.cookies.get(hub.cookie_name), token)

        if from_query or from_cookie:
            if from_query:
                location = safe_redirect_location(root, request.path)
                if location is None:
                    return web.Response(
                        status=404, text="Not Found", headers=SECURITY_HEADERS
                    )
                resp = web.Response(
                    status=303, headers={**SECURITY_HEADERS, "Location": location}
                )
                resp.set_cookie(
                    hub.cookie_name,
                    token,
                    httponly=True,
                    samesite="Strict",
                    path="/",
                )
                resp.del_cookie(hub.pair_cookie_name, path="/")
                return resp

            response = await handler(request)
            for key, value in SECURITY_HEADERS.items():
                response.headers.setdefault(key, value)
            return response

        # Approved pairing → promote to session cookie.
        pending_id = request.cookies.get(hub.pair_cookie_name)
        approved = await hub.take_approved(pending_id)
        if approved is not None:
            location = safe_redirect_location(root, request.path)
            if location is None:
                location = "/"
                # Only redirect to a real file when possible.
                loc = safe_redirect_location(root, approved.path)
                if loc is not None:
                    location = loc
            resp = web.Response(
                status=303, headers={**SECURITY_HEADERS, "Location": location}
            )
            resp.set_cookie(
                hub.cookie_name,
                token,
                httponly=True,
                samesite="Strict",
                path="/",
            )
            resp.del_cookie(hub.pair_cookie_name, path="/")
            return resp

        # HTML gets a pairing page; everything else stays opaque 403.
        if request.method == "GET" and is_html_path(request.path):
            entry = await hub.get_or_create_pending(
                pending_id,
                request.remote or "-",
                request.headers.get("User-Agent", "-"),
                request.path,
            )
            body = pair_forbidden_page(hub.port, entry.code_display)
            resp = web.Response(
                status=403,
                text=body,
                content_type="text/html",
                charset="utf-8",
                headers=SECURITY_HEADERS,
            )
            resp.set_cookie(
                hub.pair_cookie_name,
                entry.pending_id,
                httponly=True,
                samesite="Strict",
                path="/",
                max_age=PAIR_TTL_SEC,
            )
            return resp

        return web.Response(status=403, text="Forbidden", headers=SECURITY_HEADERS)

    async def serve_file(request: web.Request) -> web.StreamResponse:
        path = resolve_served_path(root, request.path)
        if path is None:
            raise web.HTTPNotFound(headers=SECURITY_HEADERS)
        return web.FileResponse(
            path,
            headers={
                **SECURITY_HEADERS,
                "Content-Type": ALLOWED_TYPES[path.suffix],
            },
        )

    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/{path:.*}", serve_file)
    return app


class _QuietAccessLogger(web.AccessLogger):
    """Log method/path/status without the query string (token lives there)."""

    def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
        try:
            path = request.path
            self.logger.info(
                '%s "%s %s HTTP/%s.%s" %s %s',
                request.remote or "-",
                request.method,
                path,
                request.version.major,
                request.version.minor,
                response.status,
                response.body_length,
            )
        except Exception:  # noqa: BLE001 — mirror aiohttp's AccessLogger
            self.logger.exception("Error in access log")


async def start_http_server(
    out_dir: Path,
    listen: str,
    port: int,
    token: str,
) -> tuple[web.AppRunner, web.SockSite, int, asyncio.AbstractServer, Path, PairingHub]:
    """Bind HTTP + pairing Unix socket; port 0 lets the OS pick a free port."""
    sock = socket.create_server((listen, port), reuse_port=False)
    bound_port = sock.getsockname()[1]
    hub = PairingHub(token, bound_port, out_dir)
    app = make_server_app(hub)
    runner = web.AppRunner(app, access_log_class=_QuietAccessLogger)
    await runner.setup()
    site = web.SockSite(runner, sock)
    await site.start()

    ipc_path = pair_socket_path(bound_port)
    if ipc_path.exists():
        ipc_path.unlink()
    unix_server = await asyncio.start_unix_server(
        hub.handle_ipc, path=str(ipc_path)
    )
    try:
        os.chmod(ipc_path, 0o600)
    except OSError:
        pass
    return runner, site, bound_port, unix_server, ipc_path, hub


async def ipc_request(sock_path: Path, payload: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=5)
        if not raw:
            return {"ok": False, "error": "empty response from server"}
        return json.loads(raw.decode())
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


def list_pair_sockets(port: int | None) -> list[Path]:
    root = state_dir()
    if port is not None:
        path = pair_socket_path(port)
        return [path] if path.exists() else []
    return sorted(root.glob("pair-*.sock"))


async def run_daemon_serve(args: argparse.Namespace) -> int:
    """Internal: long-lived HTTP server for --server (background mode)."""
    token = os.environ.get("PYTHON_TTS_TOKEN") or secrets.token_urlsafe(32)
    ready_path = os.environ.get("PYTHON_TTS_READY")
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    bind_port = args.port if args.port is not None else 0

    if args.listen in ("0.0.0.0", "::"):
        print(
            "warning: listening on all interfaces; "
            "anyone with the URL and token can read your clips",
            file=sys.stderr,
        )

    logging.getLogger("aiohttp.access").setLevel(logging.INFO)
    runner, _site, bound_port, unix_server, ipc_path, hub = await start_http_server(
        out_dir, args.listen, bind_port, token
    )
    state = {
        "pid": os.getpid(),
        "port": bound_port,
        "token": token,
        "out_dir": str(out_dir),
        "listen": args.listen,
        "hostname": args.hostname,
    }
    write_json_private(server_state_path(bound_port), state)
    if ready_path:
        write_json_private(Path(ready_path), state)

    print(
        f"daemon serving {out_dir} on port {bound_port} (pid {os.getpid()})",
        file=sys.stderr,
        flush=True,
    )
    try:
        await hub.stop_event.wait()
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        return 0
    finally:
        unix_server.close()
        await unix_server.wait_closed()
        if ipc_path.exists():
            try:
                ipc_path.unlink()
            except OSError:
                pass
        state_file = server_state_path(bound_port)
        if state_file.exists():
            try:
                state_file.unlink()
            except OSError:
                pass
        await runner.cleanup()
    return 0


def run_stop(port: int | None) -> int:
    root = state_dir()
    paths = (
        [server_state_path(port)]
        if port is not None
        else sorted(root.glob("server-*.json"))
    )
    if not paths or (port is not None and not paths[0].exists()):
        hint = f" for port {port}" if port is not None else ""
        raise SystemExit(f"No background tts server found{hint}.")

    stopped = 0
    for path in paths:
        if not path.exists():
            continue
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"skip corrupt state {path}", file=sys.stderr)
            prune_server_state(state_path=path)
            continue
        srv_port = info.get("port")
        if not isinstance(srv_port, int):
            print(f"skip {path}: no port", file=sys.stderr)
            prune_server_state(state_path=path)
            continue
        resp = ipc_call_sync(pair_socket_path(srv_port), {"op": "shutdown"}, timeout=2.0)
        if resp and resp.get("ok"):
            print(f"stopped server on port {srv_port}", file=sys.stderr)
            stopped += 1
            # Daemon removes its own files; prune leftovers if it raced.
            time.sleep(0.1)
            prune_server_state(srv_port, path)
        else:
            print(f"stale state removed for port {srv_port}", file=sys.stderr)
            prune_server_state(srv_port, path)
    if stopped == 0:
        raise SystemExit("No live servers were stopped (stale state cleared if present).")
    return 0


async def run_pair(code: str, port: int | None, assume_yes: bool) -> int:
    sockets = list_pair_sockets(port)
    if not sockets:
        hint = f" for port {port}" if port is not None else ""
        raise SystemExit(
            f"No running tts --server found{hint}. "
            "Start a server first, open the HTML page to get a pairing code, "
            "then re-run --pair."
        )

    last_err = "unknown or expired code"
    matched: tuple[Path, dict] | None = None
    for sock_path in sockets:
        try:
            resp = await ipc_request(sock_path, {"op": "lookup", "code": code})
        except (OSError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            last_err = str(exc)
            continue
        if resp.get("ok"):
            matched = (sock_path, resp)
            break
        last_err = str(resp.get("error") or last_err)

    if matched is None:
        raise SystemExit(f"Pairing failed: {last_err}")

    sock_path, info = matched
    print(
        "Pending browser:\n"
        f"  port:        {info.get('port')}\n"
        f"  remote:      {info.get('remote')}\n"
        f"  user-agent:  {info.get('user_agent')}\n"
        f"  age:         {info.get('age_sec')}s\n"
        f"  path:        {info.get('path')}",
        file=sys.stderr,
    )
    if info.get("approved"):
        print("Already approved — reload the browser page.", file=sys.stderr)
        return 0

    if not assume_yes:
        if not sys.stdin.isatty():
            raise SystemExit("Refusing to approve without a TTY; pass --yes to confirm.")
        answer = input("Approve this browser? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.", file=sys.stderr)
            return 1

    resp = await ipc_request(sock_path, {"op": "approve", "code": code})
    if not resp.get("ok"):
        raise SystemExit(f"Approve failed: {resp.get('error', 'unknown error')}")
    print("Approved. Reload the browser page.", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Synthesize speech via Microsoft Edge online TTS (personal use). "
            "Splits long input into ~1.5-minute MP3s and prints paths in order."
        ),
        epilog=(
            "Listening in a browser? Use --server and open the printed http:// URL.\n"
            '  ./tts --server -t "Hello" -p demo\n'
            "Stop with:  ./tts --stop\n"
            "Lost the token URL? Open the page, then:  ./tts --pair ABCD-EFGH"
        ),
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--text", "-t", help="Text to speak")
    src.add_argument("--file", "-f", help="Read text from file")
    p.add_argument(
        "--out-dir",
        "-o",
        default="/tmp/tts",
        help="Directory for MP3 files (default: /tmp/tts)",
    )
    p.add_argument(
        "--prefix",
        "-p",
        default="speech",
        help="Filename prefix; a timestamp is appended so runs never overwrite",
    )
    p.add_argument(
        "--voice",
        "-v",
        default=DEFAULT_VOICE,
        help=f"Edge neural voice (default: {DEFAULT_VOICE})",
    )
    p.add_argument(
        "--chunk-chars",
        type=int,
        default=DEFAULT_CHUNK_CHARS,
        help=(
            f"Max characters per chunk ≈1.5 min speech "
            f"(default: {DEFAULT_CHUNK_CHARS})"
        ),
    )
    p.add_argument(
        "--list-voices",
        action="store_true",
        help="List available voices and exit",
    )
    p.add_argument(
        "--server",
        nargs="?",
        const=0,
        default=None,
        type=int,
        metavar="PORT",
        help=(
            "Serve --out-dir over HTTP and print a tokenized http:// URL "
            "(recommended for browser playback). Reuses a live server for this "
            "--out-dir when possible. Omit PORT for a free port when starting."
        ),
    )
    p.add_argument(
        "--start-server",
        nargs="?",
        const=0,
        default=None,
        type=int,
        metavar="PORT",
        help=(
            "Like --server, but always start a new server (do not reuse). "
            "Prefer --server unless you need a fresh instance."
        ),
    )
    p.add_argument(
        "--foreground",
        action="store_true",
        help="With --start-server: keep serving in this process until Ctrl+C",
    )
    p.add_argument(
        "--stop",
        action="store_true",
        help="Stop background tts HTTP server(s)",
    )
    p.add_argument(
        "--listen",
        default=DEFAULT_LISTEN,
        help=f"Address to bind the HTTP server (default: {DEFAULT_LISTEN})",
    )
    p.add_argument(
        "--hostname",
        default=DEFAULT_HOSTNAME,
        help=(
            f"Hostname used in printed HTTP URLs "
            f"(default: {DEFAULT_HOSTNAME})"
        ),
    )
    p.add_argument(
        "--pair",
        metavar="CODE",
        help=(
            "Approve a browser pairing code shown on the 403 page "
            "(talks to a running --server via a local Unix socket)"
        ),
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Port for --pair/--stop (optional), or bind port for --daemon-serve"
        ),
    )
    p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip interactive confirmation for --pair",
    )
    p.add_argument(
        "--daemon-serve",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return p


async def list_voices() -> None:
    voices = await edge_tts.list_voices()
    for voice in sorted(voices, key=lambda v: (v["Locale"], v["ShortName"])):
        print(f"{voice['ShortName']}\t{voice['Locale']}\t{voice['Gender']}")


async def amain(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_voices:
        await list_voices()
        return 0
    if args.pair is not None:
        return await run_pair(args.pair, args.port, args.yes)
    if args.stop:
        return run_stop(args.port)
    if args.daemon_serve:
        return await run_daemon_serve(args)

    text = read_input(args)
    chunks = split_text(text, args.chunk_chars)
    if not chunks:
        raise SystemExit("No text to synthesize.")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    runner: web.AppRunner | None = None
    unix_server: asyncio.AbstractServer | None = None
    ipc_path: Path | None = None
    hub: PairingHub | None = None
    token: str | None = None
    bound_port: int | None = None
    background = False

    want_server = args.server is not None or args.start_server is not None
    force_new = args.start_server is not None
    bind_port = (
        args.start_server if force_new else args.server if args.server is not None else 0
    )

    if want_server and args.server is not None and args.start_server is not None:
        raise SystemExit("Use either --server or --start-server, not both.")

    if want_server:
        if args.listen in ("0.0.0.0", "::"):
            print(
                "warning: listening on all interfaces; "
                "anyone with the URL and token can read your clips",
                file=sys.stderr,
            )
        if args.foreground:
            if not force_new:
                raise SystemExit("--foreground requires --start-server.")
            token = secrets.token_urlsafe(32)
            logging.getLogger("aiohttp.access").setLevel(logging.INFO)
            (
                runner,
                _site,
                bound_port,
                unix_server,
                ipc_path,
                hub,
            ) = await start_http_server(
                out_dir, args.listen, int(bind_port or 0), token
            )
        else:
            info, reused = ensure_background_server(
                out_dir,
                args.listen,
                args.hostname,
                int(bind_port or 0),
                force_new=force_new,
            )
            token = str(info["token"])
            bound_port = int(info["port"])
            background = True
            if reused:
                print(
                    f"reusing server on port {bound_port}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"server on port {bound_port}",
                    file=sys.stderr,
                    flush=True,
                )

    stem = unique_stem(out_dir, args.prefix)
    html_path = write_player_html(text, [], out_dir, stem, len(chunks))
    print(html_path, flush=True)
    if token is not None and bound_port is not None:
        url = (
            f"http://{args.hostname}:{bound_port}/{html_path.name}"
            f"?token={token}"
        )
        print(url, flush=True)

    try:
        await synthesize(chunks, out_dir, args.voice, stem, text)
        if runner is not None and hub is not None and bound_port is not None:
            print(
                f"Serving {out_dir} at http://{args.hostname}:{bound_port}/ "
                "— Ctrl+C to stop.",
                file=sys.stderr,
                flush=True,
            )
            await hub.stop_event.wait()
        elif not background:
            print("hint: rerun with --server", file=sys.stderr)
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        return 0
    finally:
        if unix_server is not None:
            unix_server.close()
            await unix_server.wait_closed()
        if ipc_path is not None and ipc_path.exists():
            try:
                ipc_path.unlink()
            except OSError:
                pass
        if runner is not None:
            await runner.cleanup()
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(amain()))
    except KeyboardInterrupt:
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
