# edge-tts-cli

Turn text into speech with Microsoft Edge’s online neural voices. Long input is split into short MP3 clips (~1.5 minutes each), and a simple HTML player is written so you can listen while generation is still running.

Personal-use wrapper around [`edge-tts`](https://github.com/rany2/edge-tts).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The `./tts` script runs `tts.py` with the project venv — no need to activate it first.

### Agent skill

Coding agents can install usage instructions with:

```bash
npx skills add JLarky/python-tts
# or explicitly:
npx skills add JLarky/python-tts --skill jlarky-tts
```

That skill (`jlarky-tts`) tells the agent to clone this repo into `~/.local/share/python-tts` (override with `PYTHON_TTS_HOME`) via `skills/jlarky-tts/scripts/ensure.sh`, then run that `tts` binary—preferring `--server` for browser playback. If you previously installed it as `tts`, remove the stale skill (`npx skills remove tts`) and re-add.

## Usage

For listening in a browser, use `--server` and open the printed `http://…?token=…` URL (`file://` often won’t play audio). Reuses an existing server for the same `--out-dir` when possible. Stop with `./tts --stop`.

```bash
./tts --server -t "Hello from Edge TTS."
```

Other ways to feed text:

```bash
./tts --server --file notes.txt

echo "Piped text" | ./tts --server

# Paths only (no server) — exits when done; good for scripts
./tts --text "Hello from Edge TTS."

./tts --list-voices
```

Useful options:

| Flag | Default | Meaning |
|------|---------|---------|
| `-o` / `--out-dir` | `/tmp/tts` | Where MP3s and the HTML player go |
| `-p` / `--prefix` | `speech` | Filename prefix (a timestamp is always added) |
| `-v` / `--voice` | `en-US-EmmaMultilingualNeural` | Edge neural voice name |
| `--chunk-chars` | `1400` | Max characters per clip (~1.5 min of speech) |
| `--server [PORT]` | off | HTTP player URL; reuses a live server for this `--out-dir` |
| `--start-server [PORT]` | off | Like `--server`, but always starts a new server |
| `--stop` | — | Stop background server(s) |
| `--listen` | `127.0.0.1` | Bind address for the HTTP server |
| `--hostname` | `localhost` | Host name used in printed URLs |
| `--pair CODE` | — | Approve a browser shown on the 403 pairing page |
| `--port` | (all) | Limit `--pair` / `--stop` to a specific server port |
| `--yes` / `-y` | off | Skip confirmation for `--pair` |

## What you get

Each run creates:

1. **`{prefix}_{timestamp}.html`** — player page (printed first). It auto-refreshes every 2 seconds until all clips exist, then you can use **Play all**.
2. **`{prefix}_{timestamp}_001.mp3`**, `_002.mp3`, … — clips in order (each path printed as it finishes).

With `--server`, stdout also prints an `http://…?token=…` URL after the HTML path. Opening that URL sets an HttpOnly cookie and redirects so HTML and MP3s stay gated. Stop with `./tts --stop` (or `--stop --port N`).

Lost the token URL? Open the HTML path anyway — you’ll get a pairing page with a short code. On the machine running the server:

```bash
./tts --pair ABCD-EFGH
```

Approve the browser (or pass `--yes`), then reload. Pairing talks to the server over a local Unix socket; the short code alone does not grant access.

For LAN access (e.g. phone on the same Wi‑Fi):

```bash
./tts --server --listen 0.0.0.0 --hostname 192.168.1.20 -t "Hello from the LAN."
```

## Examples

```bash
./tts --server -t "Short demo." -o /tmp/tts -p demo

./tts --server=8765 -t "Fixed port."

./tts -f chapter.txt -v en-GB-SoniaNeural --chunk-chars 1200
```
