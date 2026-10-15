# edge-tts-cli

Turn text into speech with Microsoft Edge’s online neural voices. Long input is split into short MP3 clips (~1.5 minutes each), and a simple HTML player is written so you can listen while generation is still running.

Personal-use wrapper around [`edge-tts`](https://github.com/rany2/edge-tts).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The `./tts` script runs `tts.py` with the project venv — no need to activate it first.

## Usage

```bash
# From a string
./tts --text "Hello from Edge TTS."

# From a file
./tts --file notes.txt

# From stdin
echo "Piped text" | ./tts

# List voices
./tts --list-voices
```

Useful options:

| Flag | Default | Meaning |
|------|---------|---------|
| `-o` / `--out-dir` | `/tmp/tts` | Where MP3s and the HTML player go |
| `-p` / `--prefix` | `speech` | Filename prefix (a timestamp is always added) |
| `-v` / `--voice` | `en-US-EmmaMultilingualNeural` | Edge neural voice name |
| `--chunk-chars` | `1400` | Max characters per clip (~1.5 min of speech) |

## What you get

Each run creates:

1. **`{prefix}_{timestamp}.html`** — player page (printed first). It auto-refreshes every 2 seconds until all clips exist, then you can use **Play all**.
2. **`{prefix}_{timestamp}_001.mp3`**, `_002.mp3`, … — clips in order (each path printed as it finishes).

Open the HTML path in a browser (files live under `/tmp/tts` by default). Clips appear in the list as they finish; you do not have to wait for the whole job.

## Examples

```bash
./tts -t "Short demo." -o /tmp/tts -p demo

./tts -f chapter.txt -v en-GB-SoniaNeural --chunk-chars 1200
```
