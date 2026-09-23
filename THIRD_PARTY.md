# Third-party software, models and voices

Strawberry's own code, the crab model (`model/`, and the GLB inside the widget) and the docs are
MIT (see `LICENSE`). This file lists what Strawberry uses from others and under which licence.
It is a summary for convenience; each project's own licence text is the one that applies.

## Shipped with Strawberry

These are inside what we publish (the wheel, the sdist or the widget binary).

| What | Where it is | Licence |
|---|---|---|
| [Godot Engine](https://godotengine.org) 4.7.2 (export template) | the widget binary is a Godot export: the engine plus our project | MIT, plus the third-party components listed in Godot's [COPYRIGHT.txt](https://github.com/godotengine/godot/blob/master/COPYRIGHT.txt) (FreeType, HarfBuzz, and others; all permissive) |
| The 🍓 tray icon, rendered from [Noto Color Emoji](https://github.com/googlefonts/noto-emoji) | `src/strawberry_crab/assets/icons/` | the emoji artwork is Apache-2.0; the font file itself is SIL OFL-1.1 and is not shipped |

## Python dependencies

Installed from PyPI next to Strawberry by `uv tool install` or `pipx`; not bundled in our wheel.
Direct dependencies from `pyproject.toml`, with the licence each declares:

| Package | Licence | What for |
|---|---|---|
| [aiohttp](https://github.com/aio-libs/aiohttp) | Apache-2.0 (parts MIT) | the daemon's HTTP and websocket server |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | speech to text (pulls in CTranslate2, MIT; PyAV, BSD-3-Clause; tokenizers and huggingface-hub, Apache-2.0) |
| [jeepney](https://gitlab.com/takluyver/jeepney) | MIT | D-Bus: notifications, MPRIS, the tray (Linux only) |
| [mcp](https://github.com/modelcontextprotocol/python-sdk) | MIT | the MCP client for tool servers |
| [numpy](https://numpy.org) | BSD-3-Clause (bundled parts 0BSD, MIT, Zlib, CC0-1.0) | the beat tracker |
| [piper-tts](https://github.com/OHF-Voice/piper1-gpl) | **GPL-3.0-or-later** | text to speech (pulls in onnxruntime, MIT) |
| [sounddevice](https://github.com/spatialaudio/python-sounddevice) | MIT | Windows only: the microphone through WASAPI; its Windows wheels carry [PortAudio](https://www.portaudio.com) (MIT) (pulls in cffi, MIT-0, and pycparser, BSD-3-Clause) |
| [winrt-runtime, winrt-Windows.Foundation, winrt-Windows.Foundation.Collections, winrt-Windows.Media.Control](https://github.com/pywinrt/pywinrt) | MIT | Windows only: the System Media Transport Controls, for media (pulls in typing-extensions, PSF-2.0) |
| [winrt-Windows.UI.Notifications, winrt-Windows.UI.Notifications.Management, winrt-Windows.ApplicationModel, winrt-Windows.Storage.Streams](https://github.com/pywinrt/pywinrt) | MIT | Windows only: other apps' toasts through UserNotificationListener, with the app's name and logo, for notifications |

**Piper is GPL.** The `piper-tts` package from 1.3 on is licensed GPL-3.0-or-later (it bundles
espeak-ng). Strawberry does not copy or bundle it: the installer fetches it from PyPI as a
separate package, and Strawberry's MIT code talks to it through its public Python API. If you
redistribute an environment or image with both installed, the GPL applies to that distribution.

Optional extra `strawberry-crab[gpu]`: `nvidia-cublas-cu12` and `nvidia-cudnn-cu12`, under the
NVIDIA proprietary licence shipped in those wheels. They are only needed for whisper on CUDA. On
Windows only cuBLAS is used; CTranslate2's Windows wheel carries its own cuDNN entry DLL.

Development only (the `dev` group, not installed for users): pytest (MIT), pytest-aiohttp
(Apache-2.0), pillow (MIT-CMU, for `scripts/render_icons.py`), jeepney (MIT, so the D-Bus tests
run on every system).

## Downloaded by you, not shipped

Strawberry does not include or redistribute any model or voice. `strawberry setup` (or you, by
hand) downloads each from where its authors publish it, and you accept its terms when you do.
Setup names the licence of each model before it pulls it.

| What | Default name | Downloaded from | Licence |
|---|---|---|---|
| Ollama (the model server) | `ollama` | [ollama.com](https://ollama.com), their install script | MIT |
| The gate's embedder | `embeddinggemma` | Ollama library | [Gemma Terms of Use](https://ai.google.dev/gemma/terms) and the Gemma Prohibited Use Policy |
| The small model (her one-liners) | `gemma3:1b` | Ollama library | [Gemma Terms of Use](https://ai.google.dev/gemma/terms) and the Gemma Prohibited Use Policy |
| The brain (tools, questions) | `qwen3.8:27b` | Ollama library | Apache-2.0 is expected for the Qwen3 family; check the model card on Ollama or Hugging Face before you pull it |
| Speech recognition weights | whisper `small` (config `[voice] model`) | Hugging Face (the CTranslate2 conversions faster-whisper uses) | MIT (OpenAI Whisper weights) |
| Her voice | Piper `en_GB-alba-medium` | Hugging Face, [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) | the voice is trained on the Edinburgh "Alba" dataset, licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); see the voice's `MODEL_CARD` |

Any other model or voice you choose in the config comes with its own licence; the model card on
Ollama or Hugging Face says which.

## Optional MCP servers

None ship configured. A server you add (for example the Spotify wrapper named in `ADAPTERS.md`)
is a separate project under its own licence, installed and authorised by you.
