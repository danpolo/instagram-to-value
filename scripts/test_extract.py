"""Pure-logic unit tests for the Phase 2 extract layer. Run with:
    python3 -m pytest scripts/test_extract.py -v
No local model / live API calls -- every heavy or networked call lives inside
functions these tests don't invoke."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import secrets_lib
import ocr_gemini
import asr_qwen
import extract


def test_check_ram_guard_above_threshold():
    assert extract.check_ram_guard(8.0) is True


def test_check_ram_guard_below_threshold():
    assert extract.check_ram_guard(5.6) is False


def test_check_ram_guard_at_exact_threshold():
    assert extract.check_ram_guard(7.5, minimum_gb=7.5) is True


def test_needs_escalation_when_no_text_found():
    assert extract.needs_gemini_escalation([], []) is True


def test_needs_escalation_when_low_confidence():
    assert extract.needs_gemini_escalation(["x"], [0.3]) is True


def test_no_escalation_when_confident():
    assert extract.needs_gemini_escalation(["hello"], [0.95]) is False


def test_escalation_when_confident_but_garbled():
    # Real case: PP-OCRv6 misreading Hebrew as digit-heavy Latin noise,
    # scoring high confidence anyway (PLAN.md sec 7's Phase 2 execution notes).
    garbled = ["nn1PY,71XD 70 0171Vy 11V 17nX"]
    assert extract.needs_gemini_escalation(garbled, [0.80]) is True


def test_looks_garbled_true_for_digit_heavy_text():
    assert extract.looks_garbled(["nn1PY,71XD 70 0171Vy 11V 17nX"]) is True


def test_looks_garbled_false_for_ordinary_text():
    assert extract.looks_garbled(["Hello world, this is a headline"]) is False


def test_looks_garbled_false_for_empty_text():
    assert extract.looks_garbled([]) is False
    assert extract.looks_garbled([""]) is False


def test_detect_media_type_audio(tmp_path):
    (tmp_path / "ABC123.16k.wav").write_bytes(b"")
    assert extract.detect_media_type(tmp_path, "ABC123") == "audio"


def test_detect_media_type_image(tmp_path):
    (tmp_path / "ABC123_01.jpg").write_bytes(b"")
    assert extract.detect_media_type(tmp_path, "ABC123") == "image"


def test_detect_media_type_none(tmp_path):
    assert extract.detect_media_type(tmp_path, "ABC123") is None


def test_build_extracted_json_drops_none_fields():
    result = extract.build_extracted_json("ABC123", "audio", text="hi", reconciliation=None)
    assert result == {"shortcode": "ABC123", "media_type": "audio", "text": "hi"}


def test_should_reroute_to_caspi_on_hebrew():
    assert asr_qwen.should_reroute_to_caspi("Hebrew") is True
    assert asr_qwen.should_reroute_to_caspi("hebrew") is True  # case-insensitive


def test_should_not_reroute_on_english():
    assert asr_qwen.should_reroute_to_caspi("English") is False
    assert asr_qwen.should_reroute_to_caspi(None) is False


def test_load_secret_reads_key(tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("# comment\nGROQ_API_KEY=abc123\nGEMINI_API_KEY=xyz789\n")
    assert secrets_lib.load_secret("GROQ_API_KEY", secrets) == "abc123"
    assert secrets_lib.load_secret("GEMINI_API_KEY", secrets) == "xyz789"


def test_load_secret_missing_key_returns_none(tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("GROQ_API_KEY=\n")
    assert secrets_lib.load_secret("GROQ_API_KEY", secrets) is None


def test_load_secret_missing_file_returns_none(tmp_path):
    assert secrets_lib.load_secret("GROQ_API_KEY", tmp_path / "nope.env") is None


def test_extract_text_from_gemini_response():
    response = {
        "steps": [
            {"type": "user_input", "content": [{"type": "text", "text": "prompt"}]},
            {"type": "model_output", "content": [{"type": "text", "text": "recognized text"}]},
        ]
    }
    assert ocr_gemini.extract_text(response) == "recognized text"


def test_extract_text_missing_model_output():
    assert ocr_gemini.extract_text({"steps": []}) == ""


def test_parse_retry_after_seconds_present():
    text = 'Please retry in 49.88284644s.'
    assert ocr_gemini.parse_retry_after_seconds(text) == 49.88284644


def test_parse_retry_after_seconds_absent():
    assert ocr_gemini.parse_retry_after_seconds("some other error") is None
