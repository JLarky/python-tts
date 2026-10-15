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
```

That skill tells the agent to clone this repo into `~/.local/share/python-tts` (override with `PYTHON_TTS_HOME`) via `skills/tts/scripts/ensure.sh`, then run that `tts` binary—preferring `--start-server` for browser playback.

## Usage

For listening in a browser, use `--start-server` and open the printed `http://…?token=…` URL (`file://` often won’t play audio). The process keeps serving until Ctrl+C.

```bash
./tts --start-server -t "Hello from Edge TTS."
```

Other ways to feed text:

```bash
./tts --start-server --file notes.txt

echo "Piped text" | ./tts --start-server

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
| `--start-server [PORT]` | off | Serve `--out-dir` over HTTP (omit PORT for a free port) |
| `--listen` | `127.0.0.1` | Bind address for the HTTP server |
| `--hostname` | `localhost` | Host name used in printed URLs |

## What you get

Each run creates:

1. **`{prefix}_{timestamp}.html`** — player page (printed first). It auto-refreshes every 2 seconds until all clips exist, then you can use **Play all**.
2. **`{prefix}_{timestamp}_001.mp3`**, `_002.mp3`, … — clips in order (each path printed as it finishes).

With `--start-server`, stdout also prints an `http://…?token=…` URL after the HTML path. Opening that URL sets an HttpOnly cookie and redirects so HTML and MP3s stay gated.

For LAN access (e.g. phone on the same Wi‑Fi):

```bash
./tts --start-server --listen 0.0.0.0 --hostname 192.168.1.20 -t "Hello from the LAN."
```

## Examples

```bash
./tts --start-server -t "Short demo." -o /tmp/tts -p demo

./tts --start-server=8765 -t "Fixed port."

./tts -f chapter.txt -v en-GB-SoniaNeural --chunk-chars 1200
```
