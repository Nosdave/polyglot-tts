# Performance

## Real-Time Factor table

| Hardware | Mode | RTF (production-verified) | Notes |
|---|---|---|---|
| NVIDIA DGX Spark (Grace + Blackwell, CUDA 13) | DE/EN/FR multi-lang | 33–38× | Author's reference deployment. `docker pull :cuda` works directly — multi-arch image. |
| Consumer NVIDIA GPU (RTX 3060+, CUDA 12) | Single-lang | 20–40× | Most workstation GPUs. |
| MacBook Air M4 (CPU only) | Single-lang | ~6× | Kyutai's own published benchmark. |
| Intel/AMD Mid-Range x86 CPU | Single-lang | ~3–4× | N100-class, ~3.4 GHz boost. |
| Raspberry Pi 5 | Single-lang | ~2–3× | Pi 5 with 4 ARM Cortex-A76 cores. |
| Raspberry Pi 4 | Single-lang | ~1–2× | Barely real-time; OK for short replies. |
| HA Green (ARM64 CPU) | Single-lang | ~1–2× | Equivalent silicon to Pi 4. |

**RTF = output-audio-length / synthesis-wall-time.**
RTF 1× means real-time, RTF 30× means 30 s of audio synthesized in 1 s.

## What costs you RTF

- **Multi-language loading.** Each model loaded uses one CPU/GPU
  "instance" of the engine. Per request inference is unaffected by how
  many models are loaded — *but* RAM scales linearly.
- **Long input.** Pocket-TTS uses a delayed-streams architecture where
  latent generation runs ahead of the Mimi-decoder. Long inputs let
  GPU/CPU work in parallel and improve RTF slightly.
- **Cold start.** First synthesis after container boot includes CUDA
  kernel JIT (200–600 ms on GPU) and worker-thread spawn. `POCKET_TTS_WARMUP=true`
  (default) handles this for you.
- **Text normalization.** Negligible (<1 ms). Always leave it on.
- **Lingua LID.** ~10–30 ms per request. Comparable to the cost of
  parsing the JSON body.

## What helps RTF

- **GPU.** 5–20× the throughput of CPU on the same machine. If you have
  a GPU, use it.
- **Warmup enabled** (default).
- **Single language** if you only need one. Lower RAM, faster startup,
  no LID overhead.

## First-chunk latency

Time-To-First-Audio (TTFA) is usually more user-visible than RTF.
Polyglot is tuned for low TTFA via:

- **Sentence-buffering with `POCKET_TTS_MIN_SYNTH_CHARS`** (default 30):
  the first flush triggers as soon as we have either a sentence-ending
  punctuation mark *or* 30 accumulated characters. Lower it for faster
  first audio at the cost of less natural prosody on short replies.
- **Streaming Mimi-decoder**: audio frames are emitted as they're
  generated, not buffered until end-of-stream.
- **Mukser-Fix**: 120-sample fade-in on the first emitted frame masks
  the ConvTranspose tail-click that otherwise sounds like a glitch on
  short-reply boundary cases.

Typical TTFA on a warm GPU is 80–150 ms for the first sentence,
sub-50 ms for subsequent sentences in the same stream.

## Benchmarking your own setup

A quick rough RTF test on the HTTP endpoint:

```bash
time curl -sS -X POST http://localhost:10201/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input":"This is a fifteen second piece of text that we will measure the synthesis time for, to give us a rough real-time factor on this particular hardware setup and configuration.","voice":"eve"}' \
  --output /tmp/bench.mp3
```

Compare the `time` output's `real` value against the duration of the
generated audio (15 s in this case). `15 / 1.3 = 11.5× RTF`.

For more rigorous benchmarks, query `GET http://localhost:10299/timing`
after each synthesis — the timing side-channel returns
`{audio_ms, synth_ms, ttfa_ms}` for the most recent request.
