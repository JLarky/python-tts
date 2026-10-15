# AGENTS.md — context for coding agents

This repo is a small personal CLI around Microsoft Edge online TTS (`edge-tts`). Prefer reading this file over inventing new architecture.

## Purpose

- Input: text from `--text`, `--file`, or stdin.
- Output: ordered MP3 chunks (~1.5 min each) plus a progressive HTML player.
- Constraint: personal use of Edge online voices; do not turn this into a service, scraper, or bulk abuse tool.

## Layout

| Path | Role |
|------|------|
| `tts.py` | All logic (argparse, chunking, synthesis, HTML). |
| `tts` | Thin bash wrapper: runs `$ROOT/.venv/bin/python $ROOT/tts.py "$@"`. |
| `requirements.txt` | `edge-tts>=7.3.0`. |
| `.venv/` | Local virtualenv (gitignored). Assume it exists when using `./tts`. |

No package layout, tests, or build system beyond the venv.

## Runtime behavior (do not regress)

1. Resolve text via `read_input` — mutually exclusive `--text` / `--file`, else non-TTY stdin; otherwise exit with a clear error.
2. `split_text` collapses whitespace, splits on sentence boundaries `(?<=[.!?])\s+`, packs pieces up to `chunk-chars` (default **1400**), hard-wraps oversized sentences on words.
3. `unique_stem` builds `{prefix}_{YYYYMMDD-HHMMSS}` (suffix `_N` on collision) so runs never overwrite existing HTML/MP3s in `--out-dir`.
4. Write HTML **immediately** with 0/N clips and print its absolute path first.
5. For each chunk: `edge_tts.Communicate(chunk, voice).save(...)`, print absolute MP3 path, rewrite HTML with current clips.
6. While `ready < expected`, HTML includes `<meta http-equiv="refresh" content="2">` and a “Generating… k/N” note. When done, refresh stops and “Play all” chains `<audio>` elements via `ended`.
7. Default voice: `en-US-EmmaMultilingualNeural`. Default out dir: `/tmp/tts`.

## Conventions for changes

- Keep everything in `tts.py` unless there is a strong reason to split.
- Preserve progressive HTML updates and stdout contract: HTML path first, then one MP3 path per line as each clip finishes (`flush=True`).
- HTML uses Pico CSS from jsDelivr and inline play-all JS — keep it self-contained (no build step).
- Escape user text with `html.escape` in the player page.
- Prefer small, dependency-light changes; only add packages if clearly needed.
- Do not commit `.venv/`, `__pycache__/`, or generated MP3/HTML under `/tmp`.

## How to run / verify

```bash
./tts --list-voices          # network; lists ShortName / Locale / Gender
./tts -t "ping" -o /tmp/tts -p agent-smoke
# Expect: one .html path, then one .mp3 path; open HTML and confirm a single clip.
```

Needs network access for Edge TTS. If `.venv` is missing: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

## Out of scope unless asked

- Auth, rate limiting, or multi-user hosting.
- Subtitle/SRT generation (edge-tts can do it; this CLI does not).
- Merging MP3s into one file (by design: ordered chunks + HTML playlist).
