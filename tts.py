#!/usr/bin/env python3
"""Personal Edge TTS CLI: text → ordered MP3 chunks (~1.5 min each)."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import html
import logging
import re
import secrets
import socket
import sys
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
    # Reject scheme-relative (//host), backslashes, and absolute URLs.
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


def make_server_app(root: Path, token: str, cookie_name: str) -> web.Application:
    root = root.resolve()

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        q_token = request.rel_url.query.get("token")
        from_query = token_ok(q_token, token)
        from_cookie = token_ok(request.cookies.get(cookie_name), token)
        if not (from_query or from_cookie):
            return web.Response(status=403, text="Forbidden", headers=SECURITY_HEADERS)

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
                cookie_name,
                token,
                httponly=True,
                samesite="Strict",
                path="/",
            )
            return resp

        response = await handler(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

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
) -> tuple[web.AppRunner, web.SockSite, int]:
    """Bind HTTP server; port 0 lets the OS pick a free port."""
    sock = socket.create_server((listen, port), reuse_port=False)
    bound_port = sock.getsockname()[1]
    cookie_name = f"tts_{bound_port}"
    app = make_server_app(out_dir, token, cookie_name)
    runner = web.AppRunner(app, access_log_class=_QuietAccessLogger)
    await runner.setup()
    site = web.SockSite(runner, sock)
    await site.start()
    return runner, site, bound_port


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Synthesize speech via Microsoft Edge online TTS (personal use). "
            "Splits long input into ~1.5-minute MP3s and prints paths in order."
        ),
        epilog=(
            "Listening in a browser? Use --start-server and open the printed "
            "http:// URL.\n"
            "file:// HTML pages often won't play audio.\n"
            '  ./tts --start-server -t "Hello" -p demo\n'
            "The command keeps running (serving) after synthesis until Ctrl+C. "
            "Run it in\n"
            "the background and keep it alive while the user listens.\n"
            "Without --start-server: prints file paths and exits (good for scripts)."
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
        "--start-server",
        nargs="?",
        const=0,
        default=None,
        type=int,
        metavar="PORT",
        help=(
            "Serve --out-dir over HTTP and print a tokenized http:// URL "
            "(recommended for browser playback; file:// often fails for audio). "
            "Omit PORT for a free port. Blocks until Ctrl+C after synthesis."
        ),
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

    text = read_input(args)
    chunks = split_text(text, args.chunk_chars)
    if not chunks:
        raise SystemExit("No text to synthesize.")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    runner: web.AppRunner | None = None
    token: str | None = None
    bound_port: int | None = None

    if args.start_server is not None:
        if args.listen in ("0.0.0.0", "::"):
            print(
                "warning: listening on all interfaces; "
                "anyone with the URL and token can read your clips",
                file=sys.stderr,
            )
        token = secrets.token_urlsafe(32)
        # Quiet aiohttp access log noise to INFO with our stripper.
        logging.getLogger("aiohttp.access").setLevel(logging.INFO)
        runner, _site, bound_port = await start_http_server(
            out_dir, args.listen, args.start_server, token
        )

    stem = unique_stem(out_dir, args.prefix)
    html_path = write_player_html(text, [], out_dir, stem, len(chunks))
    print(html_path, flush=True)
    if runner is not None and token is not None and bound_port is not None:
        url = (
            f"http://{args.hostname}:{bound_port}/{html_path.name}"
            f"?token={token}"
        )
        print(url, flush=True)

    try:
        await synthesize(chunks, out_dir, args.voice, stem, text)
        if runner is not None and bound_port is not None:
            print(
                f"Serving {out_dir} at http://{args.hostname}:{bound_port}/ "
                "— keep this process running; Ctrl+C to stop.",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.Event().wait()
        else:
            print(
                "hint: file:// playback may fail in browsers; "
                "rerun with --start-server for an http:// URL.",
                file=sys.stderr,
            )
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        return 0
    finally:
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
