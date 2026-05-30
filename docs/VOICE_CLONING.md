# Voice Cloning

Polyglot TTS clones voices from a single audio sample. One clip produces
voice embeddings that work in *every* loaded language — you do not need
separate recordings per language.

## The 30-second version

1. Get a clean 10–30 second WAV of someone speaking.
2. Save it as `voices-extra/<name>.wav` (the file stem becomes the voice
   name).
3. Within a few seconds the server detects the file, encodes embeddings
   against each loaded language model, and registers the voice.
4. The voice is now available through every endpoint:
   `voice: "<name>"` in the OpenAI-Speech API, or as a Wyoming voice name.

That's the whole flow. No restart, no config edit.

## Recording recommendations

| Property | Recommended |
|---|---|
| Length | 10 to 30 seconds of speech (silence trimmed) |
| Speakers | exactly one |
| Background noise | minimal (no music, traffic, fan hum) |
| Microphone | anything decent; phone voice memo works fine |
| Sample rate | any (server resamples to 24 kHz internally) |
| Channels | mono or stereo (server downmixes) |
| Format | `.wav`, `.mp3`, `.flac`, `.m4a`, `.ogg` |
| Language | any — but a native-language sample tends to give better prosody in that language |

## Errors

If the embedder can't build embeddings (file too short, corrupt audio,
multiple speakers detected, etc.), Polyglot writes a sidecar
`<name>.wav.error` next to the source file with a short reason. Fix the
file and re-drop, or delete both and start over.

## Removing a voice

Delete the source file. Within a few seconds the file-watcher notices and
drops the voice from the registry. Any `.wav.error` sidecar is removed too.

## Replacing a voice

Overwrite the source file. The watcher re-embeds it. Voice name stays the
same; downstream callers don't need to reconnect.

## Programmatic management (REST API)

The same operations are available via HTTP on the OpenAI-Speech port
(`:10201` by default):

### List voices
```bash
curl http://polyglot-tts:10201/v1/audio/voices
```

### Upload a new voice
```bash
curl -X POST http://polyglot-tts:10201/v1/audio/voices \
  -F "file=@my_voice.wav" \
  -F "name=aria"
```

### Delete a voice
```bash
curl -X DELETE http://polyglot-tts:10201/v1/audio/voices/aria
```

Upload returns immediately with HTTP 201 and `status: queued`. Polling
`/v1/audio/voices` shortly after will show the new voice once embedding
finishes (typically within 30 seconds).

## Tips

- **Voice character travels with timbre.** A voice cloned from an English
  sample speaking French will keep an English vowel coloring. For best
  results in a specific language, clone from a native-language sample of
  that voice — if you have one.
- **Built-in voices.** All 26 Kyutai-shipped voices are available by name
  without any extra work. See `GET /v1/audio/voices` for the full list.
- **Long samples don't help.** Beyond 30 seconds, the encoder doesn't gain
  meaningful additional voice characteristics. Quality of those 30 seconds
  matters far more than length.
- **Background noise hurts.** If you can hear keyboard taps or a fan, so
  can the encoder. Try to record in a quiet room with a close mic.

## Privacy

Voices added through `voices-extra/` are local to your deployment. The
default `.gitignore` excludes them from any future git operations. The
server never sends voice data anywhere — embedding is computed locally,
the WAV stays on disk, and Polyglot does not phone home.

For shared deployments, treat the `voices-extra/` directory as you would
treat any folder of personal recordings.
