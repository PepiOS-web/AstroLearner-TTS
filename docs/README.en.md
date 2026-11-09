# AstroLearner TTS — English overview

A local Windows narration studio powered by Qwen3-TTS VoiceDesign and Base 1.7B. It provides Spanish and English profiles, reference-audio import, candidate approval, a serialized job queue, playback, downloads and persistent recent history.

This repository contains source code, tests and documentation. It does not include private recordings, generated speech, voice profiles, production scripts, downloaded weights, credentials or the installed Python environment. A fresh installation has no approved voices.

## Installation

The validated setup is Windows x64, Python 3.12, an NVIDIA RTX 5070 with 12 GB VRAM, BF16 and SDPA. Other GPUs have not been validated. Install `uv` and FFmpeg from their official sources and make them available in PATH first.

```powershell
git clone https://github.com/PepiOS-web/AstroLearner-TTS.git
cd AstroLearner-TTS
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-Studio.ps1
```

The installer creates an isolated runtime at `%LOCALAPPDATA%\AstroLearnerTTS`, downloads pinned model revisions and adds desktop start/stop shortcuts. Initial downloads require Internet; synthesis uses local models and requires no API key. The runtime is separate from this repository and the HomeLab server.

Open the desktop shortcut and visit **http://127.0.0.1:8095/**. Design an original voice or import a recording you are authorized to use. Listen to the candidate and approve its exact version before normal narration. Closing the browser does not stop the studio; use its stop shortcut.

## Limits and behavior

- Voice design: 500 characters.
- Reference import: 25 MiB, selected segment of 5–30 seconds, exact transcript up to 500 characters.
- Narration: 18,000 characters per job; maximum 500 characters per sentence.
- One active job; cooperative cancellation after the current inference step.
- Latest 20 completed results restored after restarting.
- Original 24 kHz WAV and separate 48 kHz / PCM24 loudness master when FFmpeg is available.

Voice instructions apply to VoiceDesign. Base narration uses approved reference audio and transcript. Changing a seed or resampling an export does not guarantee naturalness. The application does not train itself, include a fine-tuning workflow or guarantee real-time generation.

## Development

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-test.txt
& .\.venv\Scripts\python.exe -m pytest -q
```

The CPU suite uses synthetic temporary data and simulated models. It does not download Qwen or need private voices. Full GPU installation should use the installer and `requirements.lock`; `pip install .` alone does not reproduce the validated CUDA environment.

See the [main README](../README.md), [implementation notes](IMPLEMENTATION.md), [development history](../CHANGELOG.md) and [security guidance](../SECURITY.md). Code uses the [MIT license](../LICENSE); model, recording and voice rights remain separate.
