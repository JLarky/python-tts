# AGENTS.md — context for coding agents

This repo is a small personal CLI around Microsoft Edge online TTS (`edge-tts`). Prefer reading this file over inventing new architecture.

## Purpose

- Input: text from `--text`, `--file`, or stdin.
- Output: ordered MP3 chunks (~1.5 min each) plus a progressive HTML player.
- Constraint: personal use of Edge online voices; do not turn this into a service, scraper, or bulk abuse tool.

## Using the CLI (read this if you're just running `./tts`)

**If the user wants to listen in a browser, use `--start-server`.** Opening `file://` HTML often fails to play audio. Give the user the printed `http://…?token=…` URL, not the file path.

```bash
./tts --start-server -t "Text to read" -p demo
# or: ./tts --start-server -f notes.md -p notes
```

- The command **does not exit** after synthesis; it serves until Ctrl+C. Start it in the background (non-blocking shell or a tmux pane), wait for the `http://` line on stdout, hand that URL to the user, and leave the process running while they listen.
- The URL works right away. The page fills in clips as each one finishes.
- To reach it from another device on the LAN: `--listen 0.0.0.0 --hostname <machine-name-or-ip>`.
- Leave out `--start-server` only when the user wants MP3 files (scripts, copying, other players). In that case the command prints paths and exits.
- The token is per run, so a URL stops working once the process is stopped.

## Layout

| Path | Role |
|------|------|
| `tts.py` | All logic (argparse, chunking, synthesis, HTML, optional HTTP server). |
| `tts` | Thin bash wrapper: runs `$ROOT/.venv/bin/python $ROOT/tts.py "$@"`. |
| `requirements.txt` | `edge-tts>=7.3.0` (+ `aiohttp`). |
| `.venv/` | Local virtualenv (gitignored). Assume it exists when using `./tts`. |
| `skills/jlarky-tts/` | vercel-labs-style agent skill (`name: jlarky-tts`) for `npx skills add JLarky/python-tts`. |

No package layout, tests, or build system beyond the venv.

## Runtime behavior (do not regress)

1. Resolve text via `read_input` — mutually exclusive `--text` / `--file`, else non-TTY stdin; otherwise exit with a clear error.
2. `split_text` collapses whitespace, splits on sentence boundaries `(?<=[.!?])\s+`, packs pieces up to `chunk-chars` (default **1400**), hard-wraps oversized sentences on words.
3. `unique_stem` builds `{prefix}_{YYYYMMDD-HHMMSS}` (suffix `_N` on collision) so runs never overwrite existing HTML/MP3s in `--out-dir`.
4. Write HTML **immediately** with 0/N clips and print its absolute path first.
5. For each chunk: `edge_tts.Communicate(chunk, voice).save(...)`, print absolute MP3 path, rewrite HTML with current clips.
6. While `ready < expected`, HTML includes `<meta http-equiv="refresh" content="2">` and a “Generating… k/N” note. When done, refresh stops and “Play all” chains `<audio>` elements via `ended`.
7. Default voice: `en-US-EmmaMultilingualNeural`. Default out dir: `/tmp/tts`.
8. Optional `--start-server [PORT]` (PORT omitted → OS free port): aiohttp serves only `.html`/`.mp3` under `out_dir`. Auth is one per-run `secrets.token_urlsafe(32)` checked with `hmac.compare_digest` from `?token=` or cookie `tts_<port>`. Valid query → `303` to the same path + `Set-Cookie` (HttpOnly, SameSite=Strict, Path=/; no `Secure` — plain HTTP). Unauthenticated → `403` before path resolution. Access logs must not include the query string. Defaults: `--listen 127.0.0.1`, `--hostname localhost`. Warn on stderr when binding `0.0.0.0`/`::`. After synthesis, block until Ctrl+C. Stdout: HTML path, then `http://{hostname}:{port}/{file}?token=…`, then MP3 paths.

## Conventions for changes

- Keep everything in `tts.py` unless there is a strong reason to split.
- Preserve progressive HTML updates and stdout contract: HTML path first, then (if server) HTTP URL, then one MP3 path per line as each clip finishes (`flush=True`). Hints go to stderr only (file:// tip when no server; “keep running” when serving).
- HTML uses Pico CSS from jsDelivr and inline play-all JS — keep it self-contained (no build step). Do not embed the token in HTML; cookie covers `<audio>` requests.
- Escape user text with `html.escape` in the player page.
- Prefer small, dependency-light changes; only add packages if clearly needed. `aiohttp` is imported directly (already pulled in by `edge-tts`) for Range-capable `FileResponse`.
- Do not commit `.venv/`, `__pycache__/`, or generated MP3/HTML under `/tmp`.

## How to run / verify

```bash
./tts --list-voices          # network; lists ShortName / Locale / Gender
./tts -t "ping" -o /tmp/tts -p agent-smoke
# Expect: one .html path, then one .mp3 path; open HTML and confirm a single clip.

./tts --start-server -t "http ping" -o /tmp/tts -p agent-http
# Expect: .html path, then http://localhost:<port>/…?token=…, then .mp3 path.
# curl without token → 403; curl -L with token → 200 HTML; MP3 also 403 without cookie/token.
```

Needs network access for Edge TTS. If `.venv` is missing: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

## Out of scope unless asked

- Multi-user hosting, TLS, or long-lived shared tokens across runs.
- Subtitle/SRT generation (edge-tts can do it; this CLI does not).
- Merging MP3s into one file (by design: ordered chunks + HTML playlist).
