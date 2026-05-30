# Home Assistant Integration

Polyglot TTS speaks the Wyoming protocol on TCP port `10200` (configurable
via `POCKET_TTS_WYOMING_PORT`), which is exactly what Home Assistant's
[Wyoming integration](https://www.home-assistant.io/integrations/wyoming/)
expects.

## Setup

1. **Start Polyglot TTS** somewhere reachable from your HA host. Either on
   the same box or anywhere on the network. The simplest compose:

   ```yaml
   services:
     polyglot-tts:
       image: ghcr.io/nosdave/polyglot-tts:cuda   # or :latest for CPU
       ports:
         - "10200:10200"
       volumes:
         - ./voices-extra:/app/voices-extra
       environment:
         POCKET_TTS_LANGUAGES: "english_2026-04,german_24l,french_24l"
         POCKET_TTS_VOICE: "eve"
   ```

2. **In Home Assistant**: Settings → Devices & Services → **Add Integration**
   → search for **Wyoming Protocol** → enter the host/IP and port `10200`.

3. HA will discover Polyglot TTS, list every loaded voice and language,
   and offer it as a TTS provider.

4. **Wire it into your Voice Pipeline**: Settings → Voice Assistants →
   pick your pipeline → set **Text-to-Speech** to `pocket-tts` and choose
   a voice.

## Voice-PE / wake-word satellites

Polyglot TTS supports the [`SynthesizeChunk` text-streaming events](https://github.com/rhasspy/wyoming/blob/master/wyoming/tts.py)
introduced in HA 2025.10, so when your conversation agent supports
[`ConversationEntityFeature.STREAMING`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/conversation/models.py)
(e.g. `openai_conversation`, `ollama`, `anthropic`, `skye-harris/local_openai`),
audio starts playing on your Voice-PE while the LLM is still streaming
tokens.

Threshold for the first audio chunk is `STREAM_RESPONSE_CHARS = 60`
(HA-side) — for shorter sentences the legacy single-shot path is used.

## Languages

Each Wyoming voice is advertised with the full set of `BCP47` codes
matching the loaded checkpoints (e.g. `en`, `fr`, `de`). HA's pipeline
will send a language hint when synthesizing; Polyglot honours the hint
first, falls back to Lingua-based language detection second, and to the
default language third.

Mismatched-language texts (e.g. an English-system pipeline that briefly
asks Polyglot to speak a French sentence) automatically use the matching
voice phonetics — that's the whole point of polyglot mode.

## Voice quality tip

Voice cloning preserves *timbre* across languages but also carries some
native-language vowel coloring. Eve (English-sourced) speaking German
keeps a subtle English touch. For best results in a specific language,
clone a voice from a native sample in that language.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Voice list empty in HA | Container still loading models; wait for "Warmup complete" in logs |
| Audio cuts off mid-word | Likely the upstream HA streaming-cancel behavior; unrelated to Polyglot. File against [home-assistant/core](https://github.com/home-assistant/core/issues) with a repro if persistent. |
| Wrong language synthesized | Language hint not sent by HA + text too short for LID (under 20 chars); pass the language hint explicitly via the API, or pre-tag your intent_script responses with a `language` slot |
| First synthesis very slow | Cold-start CUDA kernel JIT; subsequent calls warm. Set `POCKET_TTS_WARMUP=true` (default) |
