"""GPU-free contract tests; optional real WAV checks use installed audio packages."""

from __future__ import annotations

import contextlib
import gc
import importlib.util
import json
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import wave
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astro_tts import audio
from astro_tts.engine import EngineBusyError, JobCancelled, TTSEngine, apply_pronunciations, normalize_text, segment_text
from astro_tts.profiles import ProfileStore


class TextTests(unittest.TestCase):
    def test_normalizes_newlines_without_losing_paragraphs(self):
        self.assertEqual(normalize_text("  Uno.\r\nDos.\r\n \r\n\r\n Tres.\t  Cuatro. "), "Uno. Dos.\n\nTres. Cuatro.")

    def test_chunks_keep_sentences_and_paragraphs(self):
        text = "Primera frase. Segunda frase. Tercera frase.\n\nOtra historia."
        chunks = segment_text(text, target=30, maximum=50)
        self.assertEqual([part.text for part in chunks], ["Primera frase. Segunda frase.", "Tercera frase.", "Otra historia."])
        self.assertEqual([part.paragraph_end for part in chunks], [False, True, True])
        self.assertEqual([part.paragraph_index for part in chunks], [0, 0, 1])

    def test_abbreviations_decimals_and_quotes(self):
        chunks = segment_text('El Dr. López midió 3.14 metros. «Todo está listo». Siguiente.', target=35)
        self.assertEqual(chunks[0].text, "El Dr. López midió 3.14 metros.")
        self.assertEqual(chunks[1].text, "«Todo está listo». Siguiente.")

    def test_refuses_long_sentence_instead_of_cutting_it(self):
        with self.assertRaisesRegex(ValueError, "Añade un punto"):
            segment_text("palabra " * 70)

    def test_literal_pronunciation_with_boundaries_no_cascade(self):
        self.assertEqual(
            apply_pronunciations("NASA y nasales; Nasa. A+B y AAB. ESA.", {"NASA": "nasa espacial", "A+B": "a más be", "ESA": "NASA"}),
            "nasa espacial y nasales; nasa espacial. a más be y AAB. NASA.",
        )

    def test_unicode_case_matching_does_not_raise(self):
        self.assertEqual(apply_pronunciations("I, i, İ, ı", {"I": "eye"}), "eye, eye, eye, eye")

    def test_pronunciations_reject_markup(self):
        with self.assertRaisesRegex(ValueError, "HTML"):
            apply_pronunciations("NASA", {"NASA": "<break/>"})


def _write_test_wave(path, waveform, sample_rate):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = b"".join(struct.pack("<h", round(value * 30000)) for value in waveform)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return {"audio_path": str(path.resolve()), "sample_rate": sample_rate, "frames": len(waveform),
            "duration_seconds": len(waveform) / sample_rate, "subtype": "FLOAT"}


def _assemble_test_wave(segments, destination, sample_rate):
    frames_written = 0
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        for segment in segments:
            with wave.open(segment["audio_path"], "rb") as source:
                frames = source.getnframes()
                segment["start_seconds"] = frames_written / sample_rate
                handle.writeframes(source.readframes(frames))
            frames_written += frames
            segment["end_seconds"] = frames_written / sample_rate
            gap = round(segment["gap_after_seconds"] * sample_rate)
            handle.writeframes(b"\x00\x00" * gap)
            frames_written += gap
    return {"audio_path": str(destination.resolve()), "sample_rate": sample_rate,
            "frames": frames_written, "duration_seconds": frames_written / sample_rate, "subtype": "FLOAT"}


class FakeCuda:
    def __init__(self, events):
        self.events = events

    def empty_cache(self):
        self.events.append("empty_cache")

    def reset_peak_memory_stats(self):
        self.events.append("reset_peak")

    def max_memory_allocated(self):
        return 1024**3


class FakeModel:
    def __init__(self, directory, calls):
        self.kind = Path(directory).name
        self.calls = calls

    def generate_voice_design(self, **kwargs):
        self.calls.append(("design", kwargs))
        return [[0.1] * 2400], 24000

    def create_voice_clone_prompt(self, **kwargs):
        self.calls.append(("prompt", kwargs))
        return [SimpleNamespace(ref_text=kwargs["ref_text"])]

    def generate_voice_clone(self, **kwargs):
        self.calls.append(("narrate", kwargs))
        return [[0.2] * 2400], 24000


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("voice-design", "base"):
            directory = self.root / "models" / name
            directory.mkdir(parents=True)
            (directory / "config.json").write_text("{}", encoding="utf-8")
        self.events = []
        self.calls = []
        self.model_refs = []
        self.fake_torch = SimpleNamespace(cuda=FakeCuda(self.events), bfloat16="BF16", inference_mode=contextlib.nullcontext)
        self.engine = TTSEngine(self.root)
        self.engine._torch = self.fake_torch
        self.engine._seed = lambda value: None

        def load(directory, **kwargs):
            gc.collect()
            self.assertFalse(any(reference() is not None for reference in self.model_refs), "Two model wrappers were resident at once")
            self.events.append(f"load:{Path(directory).name}")
            self.assertEqual(kwargs, {"device_map": "cuda:0", "dtype": "BF16", "attn_implementation": "sdpa", "local_files_only": True})
            model = FakeModel(directory, self.calls)
            self.model_refs.append(weakref.ref(model))
            return model

        self.patches = [
            patch.dict(sys.modules, {"qwen_tts": SimpleNamespace(Qwen3TTSModel=SimpleNamespace(from_pretrained=load))}),
            patch.object(audio, "write_float_wav", side_effect=_write_test_wave),
            patch.object(audio, "assemble_wav", side_effect=_assemble_test_wave),
            patch.dict("os.environ", {}),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)
        self.addCleanup(self.temporary.cleanup)

    def design(self, **extra):
        return self.engine.run_job("design", {"profile_id": "es-narrador", "text": "La luz de una estrella nos cuenta una historia.", "master": False, **extra})

    def test_design_persists_original_transcript_and_requires_approval(self):
        designed = self.design()
        status = self.engine.get_profiles()[0]
        self.assertFalse(status["approved"])
        self.assertTrue(status["draft_ready"])
        self.assertEqual(Path(designed["reference_audio_path"]).read_bytes(), Path(designed["raw_audio_path"]).read_bytes())
        reference_text = Path(designed["reference_audio_path"]).with_name("reference.txt").read_text(encoding="utf-8")
        self.assertEqual(reference_text, "La luz de una estrella nos cuenta una historia.")
        with self.assertRaisesRegex(ValueError, "aprueba"):
            self.engine.run_job("narrate", {"profile_id": "es-narrador", "text": "Una narración nueva.", "master": False})
        self.assertFalse(any(kind == "narrate" for kind, _ in self.calls))

    def test_sequential_model_unload_and_full_reference_reuse(self):
        designed = self.design()
        self.engine.approve_profile("es-narrador", candidate_id=designed["candidate_id"])
        result = self.engine.run_job("narrate", {"profile_id": "es-narrador", "text": "Un primer párrafo.\n\nUn segundo párrafo.", "master": False})
        load_design = self.events.index("load:voice-design")
        load_base = self.events.index("load:base")
        self.assertIn("empty_cache", self.events[load_design + 1:load_base])
        prompt = [kwargs for kind, kwargs in self.calls if kind == "prompt"]
        self.assertEqual(len(prompt), 1)
        self.assertEqual(prompt[0]["ref_audio"], designed["reference_audio_path"])
        self.assertIn("estrella", prompt[0]["ref_text"])
        self.assertIs(prompt[0]["x_vector_only_mode"], False)
        self.assertEqual([segment["gap_after_seconds"] for segment in result["segments"]], [0.2, 0.0])
        self.assertAlmostEqual(result["segments"][1]["start_seconds"], 0.3)
        self.assertEqual(result["timing_kind"], "chunk_boundaries_not_word_alignment")
        self.assertEqual(result["metrics"]["cuda_peak_allocated_gib"], 1.0)
        for kind, arguments in self.calls:
            if kind in {"design", "narrate"}:
                self.assertLessEqual(arguments["max_new_tokens"], 1536)

    def test_redesign_preserves_active_approved_reference_and_history(self):
        first = self.design()
        self.engine.approve_profile("es-narrador", first["candidate_id"])
        original = Path(first["reference_audio_path"]).read_bytes()
        second = self.design(seed=43)
        status = self.engine.get_profiles()[0]
        self.assertTrue(status["approved"])
        self.assertEqual(status["audio_path"], first["reference_audio_path"])
        self.assertEqual(status["draft_candidate_id"], second["candidate_id"])
        self.assertEqual(Path(first["reference_audio_path"]).read_bytes(), original)
        self.engine.approve_profile("es-narrador", second["candidate_id"])
        self.assertEqual(self.engine.get_profiles()[0]["active_candidate_id"], second["candidate_id"])
        self.assertTrue(Path(first["reference_audio_path"]).is_file())

    def test_success_releases_gpu_by_default_and_reuse_is_explicit(self):
        self.design()
        self.assertIsNone(self.engine._model)
        self.assertIsNone(self.engine._model_kind)
        self.assertEqual(self.events[-1], "empty_cache")
        self.engine._keep_model_loaded = True
        self.design()
        resident = self.engine._model
        loads_before = self.events.count("load:voice-design")
        self.design()
        self.assertIs(self.engine._model, resident)
        self.assertEqual(self.events.count("load:voice-design"), loads_before)
        del resident
        self.engine.unload()

    def test_busy_job_is_rejected_and_callback_cancellation_keeps_exception(self):
        class UserCancelled(Exception):
            pass

        def progress(message, fraction):
            with self.assertRaises(EngineBusyError):
                self.design()
            raise UserCancelled("cancelled in the UI")

        with self.assertRaisesRegex(UserCancelled, "cancelled in the UI"):
            self.engine.run_job("design", {"profile_id": "es-narrador", "master": False}, progress)
        self.assertIsNone(self.engine._model)
        self.assertTrue(self.design()["audio_path"])

    def test_cancellation_between_chunks_releases_model(self):
        self.design()
        self.engine.approve_profile("es-narrador")
        cancellation = threading.Event()

        def progress(message, fraction):
            if message.startswith("Narrando fragmento 2"):
                cancellation.set()

        with self.assertRaises(JobCancelled):
            self.engine.run_job("narrate", {"profile_id": "es-narrador", "text": "Primero.\n\nSegundo.", "master": False, "cancel_event": cancellation}, progress)
        self.assertEqual(len([call for call in self.calls if call[0] == "narrate"]), 1)
        self.assertIsNone(self.engine._model)
        self.assertFalse(self.engine._job_lock.locked())

    def test_invalid_profile_and_oversized_text_do_not_load_models(self):
        with self.assertRaises(ValueError):
            self.engine.run_job("design", {"profile_id": "../another-folder", "master": False})
        with self.assertRaises(ValueError):
            self.engine.run_job("narrate", {"profile_id": "es-narrador", "text": "a" * 18001, "master": False})
        self.assertEqual(self.model_refs, [])

    def test_doctor_does_not_import_torch_or_qwen(self):
        self.engine._torch = None
        original_import = __import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"torch", "qwen_tts"}:
                raise AssertionError("doctor imported a heavy model dependency")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import), patch("shutil.which", return_value=None):
            status = self.engine.doctor()
        self.assertFalse(status["ready"])
        self.assertEqual(status["mode"], "cuda-bfloat16-sdpa")


_HAS_AUDIO = importlib.util.find_spec("numpy") is not None and importlib.util.find_spec("soundfile") is not None


@unittest.skipUnless(_HAS_AUDIO, "NumPy/SoundFile are optional for the standard-library contract tests")
class RealAudioTests(unittest.TestCase):
    def test_float_original_preserves_peak_and_assembly_gap_is_sample_accurate(self):
        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            waveform = np.array([0.0, 1.2, -1.1, 0.2], dtype=np.float32)
            first = audio.write_float_wav(directory / "first.wav", waveform, 24000)
            decoded, rate = sf.read(first["audio_path"], dtype="float32")
            np.testing.assert_array_equal(decoded, waveform)
            self.assertEqual(rate, 24000)
            self.assertEqual(sf.info(first["audio_path"]).subtype, "FLOAT")
            second = audio.write_float_wav(directory / "second.wav", waveform, 24000)
            segments = [{"audio_path": first["audio_path"], "gap_after_seconds": 0.12},
                        {"audio_path": second["audio_path"], "gap_after_seconds": 0.0}]
            result = audio.assemble_wav(segments, directory / "joined.wav", 24000)
            self.assertEqual(result["frames"], 8 + 2880)
            self.assertAlmostEqual(segments[1]["start_seconds"], (4 + 2880) / 24000)

    def test_nonfinite_waveform_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(audio.AudioError):
                audio.write_float_wav(Path(temporary) / "bad.wav", [float("nan")], 24000)


if __name__ == "__main__":
    unittest.main()
