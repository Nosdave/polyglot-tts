# Performance

## Real-Time Factor table

**RTF = output-audio-length / synthesis-wall-time.** RTF 1× means real-time
(audio generated as fast as it plays); RTF 5× means 5 s of audio in 1 s.
For streaming voice, anything ≥1× means no playback lag.

Only the **measured** rows below are from real hardware. The rest are
**rough estimates** and almost certainly imprecise — if you run Polyglot
on one of these and measure, please open a PR with real numbers.

| Hardware | Mode | RTF | Source |
|---|---|---|---|
| NVIDIA DGX Spark (Grace + Blackwell GB10, sm_121, CUDA 13) | DE single-lang, GPU | **~5×** | **Measured** (cu128 build; see note below) |
| DGX Spark CPU **(under heavy load)** | DE single-lang | **~0.24×** | **Measured** (box was saturated by other workloads — not representative of an idle CPU) |
| MacBook Air M4 (CPU only) | Single-lang | ~6× | Kyutai's published benchmark |
| Consumer NVIDIA GPU (RTX 3060+, CUDA 12) | Single-lang | ~20–40× (estimate) | Unverified |
| Intel/AMD mid-range x86 CPU (N100-class) | Single-lang | ~3–4× (estimate) | Unverified |
| Raspberry Pi 5 (4× Cortex-A76) | Single-lang | ~1–2× (estimate) | Unverified — likely the practical floor |
| Raspberry Pi 4 / HA Green (ARM64) | Single-lang | ~0.5–1× (estimate) | Unverified — may be below real-time |

### Note on Blackwell GB10 (DGX Spark) RTF

The measured ~5× on Spark is **lower than you'd expect from a Blackwell
GPU**. The `:cuda` image is built against PyTorch cu128, whose bundled
nvrtc does not natively know sm_121 (GB10), so kernels fall back to
runtime-JIT/Triton paths. Native sm_121 kernels (via a cu130 build) should
lift this — tracked in
[issue #1](https://github.com/Nosdave/polyglot-tts/issues/1). ~5× is still
comfortably faster than real-time, so streaming voice has no lag today.

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
