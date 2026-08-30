"""Pure-logic unit tests for the Phase 2 extract layer. Run with:
    python3 -m pytest scripts/test_extract.py -v
No local model / live API calls -- every heavy or networked call lives inside
functions these tests don't invoke."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import secrets_lib
import ocr_gemini
import ocr_ocrspace
import reconcile
import asr_qwen
import extract


def test_check_ram_guard_above_threshold():
    assert extract.check_ram_guard(8.0) is True


def test_check_ram_guard_below_threshold():
    assert extract.check_ram_guard(5.6) is False


def test_check_ram_guard_at_exact_threshold():
    assert extract.check_ram_guard(7.5, minimum_gb=7.5) is True


def test_needs_escalation_when_no_text_found():
    assert extract.needs_escalation([], []) is True


def test_needs_escalation_when_low_confidence():
    assert extract.needs_escalation(["x"], [0.3]) is True


def test_no_escalation_when_confident():
    assert extract.needs_escalation(["hello"], [0.95]) is False


def test_escalation_when_confident_but_garbled():
    # Real case: PP-OCRv6 misreading Hebrew as digit-heavy Latin noise,
    # scoring high confidence anyway (PLAN.md sec 7's Phase 2 execution notes).
    garbled = ["nn1PY,71XD 70 0171Vy 11V 17nX"]
    assert extract.needs_escalation(garbled, [0.80]) is True


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


# --- Gemini model rotation (PLAN.md sec 7's Phase 2 quota-wall fix) ---


def test_gemini_rotation_is_ordered_by_measured_accuracy():
    # 3.5-flash was exact on every run; flash-lite dropped a letter and is the
    # deliberate last resort. Anything <=3.1 is excluded for hallucinating.
    assert ocr_gemini.GEMINI_MODELS[0] == "gemini-3.5-flash"
    assert ocr_gemini.GEMINI_MODELS[-1] == "gemini-3.5-flash-lite"
    assert not any(m.startswith(("gemini-2.", "gemini-3.1")) for m in ocr_gemini.GEMINI_MODELS)


def test_plan_round_wait_honors_longest_api_suggestion():
    assert ocr_gemini.plan_round_wait([12.0, 49.0, None], attempt=1) == 50.0


def test_plan_round_wait_falls_back_to_backoff():
    assert ocr_gemini.plan_round_wait([None, None], attempt=3) == 8


def test_build_payload_carries_model_and_image():
    payload = ocr_gemini.build_payload(__file__, "gemini-3.5-flash")
    assert payload["model"] == "gemini-3.5-flash"
    assert payload["input"][1]["type"] == "image"


# --- OCR.space Engine 3 wrapper ---


def test_strip_engine_markers_removes_wrapper():
    # Engine 3 wraps some responses and not others -- observed on slides 04/05
    # of DcgAqIADbs2 but not 01-03.
    raw = "--- OCR Start ---\nשלום עולם\n--- OCR End ---"
    assert ocr_ocrspace.strip_engine_markers(raw) == "שלום עולם"


def test_strip_engine_markers_leaves_unwrapped_text_alone():
    assert ocr_ocrspace.strip_engine_markers("שלום עולם") == "שלום עולם"


def test_strip_engine_markers_keeps_interior_paragraph_breaks():
    raw = "--- OCR Start ---\nline one\n\nline two\n--- OCR End ---"
    assert ocr_ocrspace.strip_engine_markers(raw) == "line one\n\nline two"


def test_parse_ocrspace_response_success():
    payload = {"OCRExitCode": 1, "IsErroredOnProcessing": False,
               "ParsedResults": [{"ParsedText": "hello"}]}
    assert ocr_ocrspace.parse_ocrspace_response(payload) == ("hello", None)


def test_parse_ocrspace_response_partial_success_still_returns_text():
    payload = {"OCRExitCode": 2, "IsErroredOnProcessing": False,
               "ParsedResults": [{"ParsedText": "partial"}]}
    text, error = ocr_ocrspace.parse_ocrspace_response(payload)
    assert (text, error) == ("partial", None)


def test_parse_ocrspace_response_error_message_as_list():
    # Real shape returned when asking Engine 2 for Hebrew.
    payload = {"OCRExitCode": 3, "IsErroredOnProcessing": True,
               "ErrorMessage": ["E201: Value for parameter 'language' is invalid"]}
    text, error = ocr_ocrspace.parse_ocrspace_response(payload)
    assert text is None
    assert "E201" in error


def test_parse_ocrspace_response_no_results():
    payload = {"OCRExitCode": 1, "IsErroredOnProcessing": False, "ParsedResults": []}
    text, error = ocr_ocrspace.parse_ocrspace_response(payload)
    assert text is None and error


def test_parse_rate_limit_seconds():
    body = "You may only perform this action upto maximum 10 number of times within 600 seconds"
    assert ocr_ocrspace.parse_rate_limit_seconds(body) == 600.0
    assert ocr_ocrspace.parse_rate_limit_seconds("some other error") is None


def test_needs_downscale_at_free_tier_cap():
    assert ocr_ocrspace.needs_downscale(1024 * 1024) is False
    assert ocr_ocrspace.needs_downscale(1024 * 1024 + 1) is True
    assert ocr_ocrspace.needs_downscale(421_809) is False  # largest slide on disk


# --- escalation engine choice (graceful degradation) ---


def test_choose_escalation_prefers_ocrspace_and_keeps_gemini_for_reconciliation():
    ocrspace = {"engine": "ocrspace-engine3", "text": "a"}
    gemini = {"engine": "gemini-3.5-flash", "text": "b"}
    assert extract.choose_escalation(ocrspace, gemini) == (ocrspace, gemini)


def test_choose_escalation_falls_back_to_gemini_when_ocrspace_down():
    gemini = {"engine": "gemini-3.5-flash", "text": "b"}
    assert extract.choose_escalation(None, gemini) == (gemini, None)


def test_choose_escalation_single_engine_has_nothing_to_reconcile():
    ocrspace = {"engine": "ocrspace-engine3", "text": "a"}
    assert extract.choose_escalation(ocrspace, None) == (ocrspace, None)


def test_choose_escalation_total_failure_returns_none():
    # The caller is what fails loudly; the decision itself stays pure.
    assert extract.choose_escalation(None, None) == (None, None)


def test_choose_escalation_demotes_empty_ocrspace_to_gemini():
    # A call that succeeded but recognized nothing must not outrank a real read,
    # and must not be reconciled against (reconcile.py exits 1 on an empty side).
    empty = {"engine": "ocrspace-engine3", "text": "   "}
    gemini = {"engine": "gemini-3.5-flash", "text": "real text"}
    assert extract.choose_escalation(empty, gemini) == (gemini, None)


def test_choose_escalation_drops_empty_gemini_as_corroborator():
    ocrspace = {"engine": "ocrspace-engine3", "text": "real text"}
    empty = {"engine": "gemini-3.5-flash", "text": ""}
    assert extract.choose_escalation(ocrspace, empty) == (ocrspace, None)


def test_choose_escalation_keeps_empty_result_when_neither_found_text():
    # "This slide genuinely has no text" is an answer, not a failure.
    empty = {"engine": "ocrspace-engine3", "text": ""}
    authoritative, corroborating = extract.choose_escalation(empty, None)
    assert authoritative is empty and corroborating is None


def test_has_text_distinguishes_empty_from_missing():
    assert extract.has_text({"text": "x"}) is True
    assert extract.has_text({"text": "  \n "}) is False
    assert extract.has_text({}) is False
    assert extract.has_text(None) is False


# --- reconcile.py reused for OCR with explicit labels ---


def test_reconcile_labels_default_to_audio_names():
    result = reconcile.reconcile("the cat", "the hat")
    assert "local_word_count" in result and "groq_word_count" in result
    assert result["disagreement_spans"][0]["local"] == "cat"


def test_reconcile_accepts_ocr_labels():
    result = reconcile.reconcile("the cat", "the hat", label_a="ocrspace", label_b="gemini")
    assert result["ocrspace_word_count"] == 2
    span = result["disagreement_spans"][0]
    assert span["ocrspace"] == "cat" and span["gemini"] == "hat"


def test_reconcile_identical_text_is_full_agreement():
    result = reconcile.reconcile("שלום עולם", "שלום עולם", label_a="ocrspace", label_b="gemini")
    assert result["agreement_ratio"] == 1.0
    assert result["disagreement_spans"] == []
