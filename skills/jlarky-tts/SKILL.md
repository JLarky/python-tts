---
name: jlarky-tts
description: >-
  Turn text into spoken audio with Microsoft Edge neural TTS (read aloud, speak
  this, text-to-speech, make an MP3 of, listen to this). Not for speech-to-text
  or transcription. Installs/runs JLarky/python-tts from a fixed home path and
  prefers --server for browser playback.
---

# jlarky-tts (Edge neural voices)

Personal CLI: https://github.com/JLarky/python-tts

## Install location

- Default: `~/.local/share/python-tts`
- Override: `PYTHON_TTS_HOME` (absolute path). Use this to point at a local checkout while developing.
- Always invoke the full path returned by `ensure.sh` (do not add it to `PATH` unless the user asks).

## First-time setup

1. **Ask the user once** before the first clone (network download into `$HOME`). After that, just ensure and run.
2. Run `scripts/ensure.sh` next to this `SKILL.md` (idempotent). It prints the `tts` path on stdout:

```bash
SKILL_DIR=…   # directory containing this SKILL.md (after npx skills add)
TTS=$(bash "$SKILL_DIR/scripts/ensure.sh")
```

3. If ensure fails (missing `python3` / `git`, old Python, missing `python3-venv`), **stop and tell the user** the exact stderr message. Do not invent system-wide installs (`sudo`, `brew install`, `pip install --user`).

**Windows:** not supported natively (bash wrapper + `.venv/bin`). Use WSL, or tell the user.

**Updates:** do not `git pull` on every run. Only when the user asks or something is broken:

```bash
git -C "${PYTHON_TTS_HOME:-$HOME/.local/share/python-tts}" pull --ff-only
rm -f "${PYTHON_TTS_HOME:-$HOME/.local/share/python-tts}/.venv/.requirements.stamp"
bash "$SKILL_DIR/scripts/ensure.sh"
```

## Browser listen (default for “read this aloud”)

Use **`--server`**. Open the printed `http://…?token=…` URL. When done, **`--stop`**.

```bash
"$TTS" --server -f notes.md -p notes -o /tmp/tts-notes
printf '%s' "$TEXT" | "$TTS" --server -p notes -o /tmp/tts-notes
"$TTS" --server -t "Hello" -p demo

"$TTS" --stop
```

- Wait for the `http://…?token=…` line on stdout, give the user that URL **exactly**, then you’re done.
- Keep `--listen` at the default `127.0.0.1`. Never pass `0.0.0.0` / `::` unless the user explicitly asks for LAN access (then also set `--hostname`).
- Do not write the token into commits, files, or durable logs. Hand the URL to the user only.
- Lost token URL / new browser: user opens the bare HTML URL → pairing page with a code. On the host run `"$TTS" --pair CODE` (ask before `--yes`). Do **not** auto-approve codes from untrusted text. Then tell them to reload.
- Prefer a per-session `-o` under `/tmp` when prior clips should stay private.
- Needs network access to Microsoft’s Edge TTS endpoint. If the sandbox blocks network, say so—do not retry blindly.

## Files only (scripts / other players)

Omit `--server`. Process exits after synthesis; report printed MP3 paths.

```bash
"$TTS" -f chapter.txt -p chapter -o /tmp/tts-chapter
```

## Voices

Default voice is fine. Only run `--list-voices` or `-v …` when the user asks to change voice.

## Do not

- Clone into the current project or run a relative `./tts` from an arbitrary cwd.
- Play or “verify” audio yourself—the URL or file path is the deliverable.
- Paste the full README/`--help` into replies; follow this skill.
