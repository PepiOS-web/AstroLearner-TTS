"""Original AstroLearner voice specifications and versioned local references.

This module uses only the standard library. A new design is always a candidate;
approval changes a small index and never overwrites an earlier reference.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROFILE_SPECS: dict[str, dict[str, Any]] = {
    "es-narrador": {
        "id": "es-narrador",
        "name": "AstroLearner · Narrador español",
        "language": "Spanish",
        "description": "Voz masculina adulta, cálida y serena; español de España y curiosidad científica.",
        "instruct": (
            "An original native male documentary narrator from Madrid, Spain, speaking standard "
            "central-northern Peninsular Spanish. Clear Castilian distinction: pronounce z and c before "
            "e or i as the voiceless dental fricative /θ/, distinct from s /s/. Natural educated Spanish "
            "from Spain, warm mid-low voice, conversational intonation, clear vowels and consonants, "
            "moderate pace, thoughtful pauses and restrained scientific curiosity. Consistent accent throughout."
        ),
        "reference_text": (
            "La luz de una estrella puede viajar durante miles de años antes de llegar a nuestros ojos. "
            "Al observar el universo, también miramos hacia el pasado. Cada descubrimiento nos invita "
            "a hacer una nueva pregunta, con calma, curiosidad y el deseo de comprender nuestro lugar en el cosmos."
        ),
        "acceptance_notes": "Comprueba acento, entonación y distinción de s frente a z/ce/ci; la descripción orienta al modelo y no garantiza el resultado.",
    },
    "en-storyteller": {
        "id": "en-storyteller",
        "name": "AstroLearner · English storyteller",
        "language": "English",
        "description": "Narrador masculino mayor, cercano y reflexivo; inglés claro con calidez y expresión contenida.",
        "instruct": (
            "An original older adult male storyteller speaking clear natural English. Warm low-to-mid register, "
            "a gentle lived-in texture, friendly and approachable, thoughtful pauses, understated enthusiasm "
            "and expressive but restrained phrasing. The feeling is a patient science storyteller explaining "
            "something fascinating to one listener. Neutral American English is a starting preference to audition, "
            "not an assertion about any existing voice. Avoid exaggerated frailty, breathiness, rasp, comedy, "
            "theatrical booming, radio-advertisement cadence, whispering or impersonating a particular person. "
            "Maintain clear scientific pronunciation and a steady comfortable pace."
        ),
        "reference_text": (
            "Light from a distant star may travel for thousands of years before it reaches our eyes. "
            "When we look into the universe, we also look into the past. Every discovery invites another "
            "question, and a quiet moment of wonder about our place among the stars."
        ),
        "acceptance_notes": "Listen for natural delivery and pronunciation before approving this original character voice.",
    },
}

_CANDIDATE_ID = re.compile(r"[a-f0-9]{32}\Z")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_profile_spec(profile_id: str) -> dict[str, Any]:
    if profile_id not in PROFILE_SPECS:
        raise ValueError("Perfil desconocido. Elige es-narrador o en-storyteller.")
    return copy.deepcopy(PROFILE_SPECS[profile_id])


def write_json(path: Path, value: Any) -> None:
    """Replace one JSON document atomically, including when readers are polling."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ProfileStore:
    def __init__(self, runtime_dir: Path):
        self.root = Path(runtime_dir).resolve() / "profiles"

    def _directory(self, profile_id: str) -> Path:
        get_profile_spec(profile_id)
        return self.root / profile_id

    def _index(self, profile_id: str) -> dict[str, Any]:
        path = self._directory(profile_id) / "profile.json"
        if not path.is_file():
            return {
                "schema_version": 1,
                "profile_id": profile_id,
                "active_candidate_id": None,
                "latest_candidate_id": None,
            }
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"No se pudo leer el índice del perfil {profile_id}.") from exc
        if not isinstance(value, dict) or value.get("profile_id") != profile_id:
            raise ValueError(f"Índice de perfil no válido: {profile_id}.")
        return value

    def candidate(self, profile_id: str, candidate_id: str) -> dict[str, Any]:
        if not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(candidate_id):
            raise ValueError("Identificador de candidato no válido.")
        directory = self._directory(profile_id) / "candidates" / candidate_id
        try:
            metadata = json.loads((directory / "profile.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("No se encuentra la referencia de voz seleccionada.") from exc
        if metadata.get("profile_id") != profile_id or metadata.get("candidate_id") != candidate_id:
            raise ValueError("Los datos de la referencia de voz no coinciden con el perfil.")
        audio_path = directory / "reference.wav"
        transcript_path = directory / "reference.txt"
        if not audio_path.is_file() or not transcript_path.is_file():
            raise ValueError("La referencia de voz está incompleta; vuelve a diseñar el perfil.")
        return {
            **metadata,
            "audio_path": str(audio_path.resolve()),
            "transcript_path": str(transcript_path.resolve()),
            "reference_text": transcript_path.read_text(encoding="utf-8"),
        }

    def status(self, profile_id: str) -> dict[str, Any]:
        spec = get_profile_spec(profile_id)
        index = self._index(profile_id)
        active_id = index.get("active_candidate_id")
        latest_id = index.get("latest_candidate_id")
        active = self.candidate(profile_id, active_id) if active_id else None
        latest = self.candidate(profile_id, latest_id) if latest_id else None
        approved = bool(active and active.get("approved"))
        has_draft = bool(latest and latest_id != active_id)
        return {
            **spec,
            "approved": approved,
            "reference_ready": approved,
            "audio_path": active["audio_path"] if active else None,
            "active_candidate_id": active_id,
            "draft_ready": has_draft,
            "draft_audio_path": latest["audio_path"] if has_draft else None,
            "draft_candidate_id": latest_id if has_draft else None,
            "latest_candidate_id": latest_id,
            "approved_at": active.get("approved_at") if active else None,
        }

    def list(self) -> list[dict[str, Any]]:
        return [self.status(profile_id) for profile_id in PROFILE_SPECS]

    def save_candidate(
        self,
        profile_id: str,
        audio_path: Path,
        text: str,
        metadata: dict[str, Any],
        *,
        source: str = "local_original_voice_design",
    ) -> dict[str, Any]:
        spec = get_profile_spec(profile_id)
        if source not in {"local_original_voice_design", "local_authorized_reference_import"}:
            raise ValueError("Origen de referencia no válido.")
        # Read before creating anything: a damaged index must not discard history.
        index = self._index(profile_id)
        candidate_id = uuid.uuid4().hex
        directory = self._directory(profile_id) / "candidates" / candidate_id
        directory.mkdir(parents=True, exist_ok=False)
        shutil.copy2(audio_path, directory / "reference.wav")
        (directory / "reference.txt").write_text(text, encoding="utf-8", newline="\n")
        candidate = {
            **metadata,
            "schema_version": 1,
            "profile_id": profile_id,
            "candidate_id": candidate_id,
            "name": spec["name"],
            "language": spec["language"],
            "source": source,
            "created_at": utc_now(),
            "approved": False,
            "reference_audio": "reference.wav",
            "reference_transcript": "reference.txt",
        }
        write_json(directory / "profile.json", candidate)
        index["latest_candidate_id"] = candidate_id
        index["updated_at"] = utc_now()
        write_json(self._directory(profile_id) / "profile.json", index)
        return self.candidate(profile_id, candidate_id)

    def approve(self, profile_id: str, candidate_id: str | None = None) -> dict[str, Any]:
        index = self._index(profile_id)
        selected = candidate_id or index.get("latest_candidate_id")
        if not selected:
            raise ValueError("Primero genera y escucha un candidato de voz.")
        if selected != index.get("latest_candidate_id"):
            raise ValueError("Este borrador ha cambiado. Escucha el candidato actual antes de aprobarlo.")
        candidate = self.candidate(profile_id, selected)
        metadata_path = self._directory(profile_id) / "candidates" / selected / "profile.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["approved"] = True
        metadata["approved_at"] = utc_now()
        write_json(metadata_path, metadata)
        index["active_candidate_id"] = candidate["candidate_id"]
        index["updated_at"] = utc_now()
        write_json(self._directory(profile_id) / "profile.json", index)
        return self.status(profile_id)

    def reference(self, profile_id: str, allow_unreviewed: bool = False) -> dict[str, Any]:
        index = self._index(profile_id)
        selected = index.get("active_candidate_id")
        if selected:
            candidate = self.candidate(profile_id, selected)
            if candidate.get("approved"):
                return candidate
        if allow_unreviewed and index.get("latest_candidate_id"):
            return self.candidate(profile_id, index["latest_candidate_id"])
        raise ValueError("Diseña, escucha y aprueba una referencia de este perfil antes de narrar.")
