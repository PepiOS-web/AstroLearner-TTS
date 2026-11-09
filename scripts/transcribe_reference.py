"""Transcribe a short local reference without uploading its audio anywhere.

This optional preparation utility downloads the official Whisper-small model
at a resolved commit, then uses the existing Torch/Transformers environment.
It does not approve a voice or change a profile.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
from pathlib import Path

ASR_REPOSITORY = "openai/whisper-small"
ASR_REVISION = "973afd24965f72e36ca33b3055d56a652f456b4d"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--language", choices=("spanish", "english"), default="spanish")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or not 1 <= args.seconds <= 30:
        parser.error("Usa entre 1 y 30 segundos por archivo.")
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    from huggingface_hub import snapshot_download

    model_dir = args.runtime / "models" / "whisper-small"
    lock_path = model_dir / "astro-model-lock.json"
    if lock_path.is_file():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        if lock != {"repo_id": ASR_REPOSITORY, "revision": ASR_REVISION}:
            raise ValueError("La revisión del transcriptor no coincide con la versión fijada.")
    else:
        lock = {"repo_id": ASR_REPOSITORY, "revision": ASR_REVISION}
        print(json.dumps({"stage": "download_asr_model", **lock}), flush=True)
        snapshot_download(
            repo_id=lock["repo_id"], revision=lock["revision"], token=False,
            local_dir=model_dir,
            allow_patterns=["*.json", "*.txt", "model.safetensors"],
        )
        lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")

    import numpy as np
    import soundfile as sf
    import torch
    from scipy.signal import resample_poly
    from transformers import pipeline

    torch.set_num_threads(4)
    print(json.dumps({"stage": "loading_local_asr", "device": "cpu"}), flush=True)
    recognizer = pipeline(
        "automatic-speech-recognition", model=str(model_dir), device=-1,
        dtype=torch.float32, model_kwargs={"local_files_only": True},
    )
    records = []
    for path in args.input:
        info = sf.info(path)
        wave, rate = sf.read(path, frames=int(args.seconds * info.samplerate), dtype="float32", always_2d=True)
        wave = wave.mean(axis=1)
        divisor = math.gcd(rate, 16000)
        wave = resample_poly(wave, 16000 // divisor, rate // divisor).astype(np.float32)
        result = recognizer(
            {"raw": wave, "sampling_rate": 16000}, return_timestamps=True,
            generate_kwargs={"language": args.language, "task": "transcribe", "do_sample": False},
        )
        records.append({"source_path": str(path.resolve()), "source_seconds": info.duration,
                        "transcribed_seconds": len(wave) / 16000, "language": args.language,
                        "asr_model": lock, **result})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"stage": "transcribed", "source": path.name, "text": result["text"]}, ensure_ascii=True), flush=True)
    del recognizer
    gc.collect()


if __name__ == "__main__":
    main()
