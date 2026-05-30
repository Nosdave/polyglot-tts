# voices-extra/

This directory is mounted at runtime as the user-voice drop-zone.

## How to add a custom voice

1. Record (or obtain) a clean voice sample — 10 to 30 seconds of speech,
   one speaker, low background noise. Any sample rate works (the server
   resamples internally to 24 kHz).
2. Copy the file to `voices-extra/<voice-name>.wav`.
3. Within a few seconds the server detects the new file and computes
   voice embeddings against every loaded language model. This takes
   roughly 10–30 seconds depending on hardware.
4. Once you see `Voice '<voice-name>' ready` in the server log, the
   voice is available under that name through every endpoint
   (Wyoming, OpenAI-HTTP).

## Supported formats

`.wav`, `.mp3`, `.flac`, `.m4a`, `.ogg`

## Removing a voice

Delete the source file. The server detects the removal and drops the
voice from its registry within a few seconds.

## Errors

If embedding fails (file too short, unsupported codec, multiple speakers,
etc.) the server writes a sidecar file `<voice-name>.wav.error` with the
reason. Fix the source and re-drop.

## Files in this directory are not tracked by git

`.gitignore` excludes every audio file in here. Your voices stay local.
