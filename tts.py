#!/usr/bin/env python3
"""Personal Edge TTS CLI: text → ordered MP3 chunks (~1.5 min each)."""

from __future__ import annotations

import argparse
import asyncio
import html
import re
import sys
from datetime import datetime
from pathlib import Path

import edge_tts

# English speech is ~140–160 wpm; ~5 chars/word + spaces ≈ 750–900 chars/min.
# 1.5 minutes ≈ 1100–1350 chars. Default 1400 leaves a little headroom.
DEFAULT_CHUNK_CHARS = 1400
DEFAULT_VOICE = "en-US-EmmaMultilingualNeural"

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
WHITESPACE = re.compile(r"\s+")


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Synthesize speech via Microsoft Edge online TTS (personal use). "
            "Splits long input into ~1.5-minute MP3s and prints paths in order."
        )
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
    stem = unique_stem(out_dir, args.prefix)
    html_path = write_player_html(text, [], out_dir, stem, len(chunks))
    print(html_path, flush=True)
    await synthesize(chunks, out_dir, args.voice, stem, text)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
