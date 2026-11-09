"""Audition a draft reference on fresh text without approving the profile.

Stop the desktop studio first so only one process owns the voice workspace.
This preparation utility is separate from the normal approved-voice workflow.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astro_tts.engine import TTSEngine


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--profile", choices=("es-narrador", "en-storyteller"), default="es-narrador")
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=73)
    args = parser.parse_args()
    with socket.socket() as check:
        check.settimeout(1)
        if check.connect_ex(("127.0.0.1", 8095)) == 0:
            parser.error("Detén el estudio local antes de generar esta audición independiente.")
    engine = TTSEngine(args.runtime)
    try:
        profile = next(p for p in engine.get_profiles() if p["id"] == args.profile)
        if profile["approved"]:
            parser.error("Esta utilidad prepara la primera audición sin aprobar. Para una voz aprobada, usa Narrar en el estudio.")
        if not profile["draft_ready"]:
            parser.error("Prepara primero una referencia de voz para este perfil.")
        def progress(message: str, fraction: float) -> None:
            print(json.dumps({"message": message, "fraction": fraction}, ensure_ascii=True), flush=True)

        result = engine.run_job("narrate", {
            "profile_id": args.profile,
            "text": args.text_file.read_text(encoding="utf-8"),
            "seed": args.seed,
            "allow_unreviewed": True,
            "master": True,
        }, progress)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"stage": "audition_ready", "profile_id": args.profile,
                          "candidate_id": result["candidate_id"], "approved": result["approved"],
                          "audio_path": result["audio_path"], "duration_seconds": result["duration_seconds"]},
                         ensure_ascii=True), flush=True)
    finally:
        engine.unload()


if __name__ == "__main__":
    main()
