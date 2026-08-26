"""Pure-logic unit tests for the Phase 2 extract layer. Run with:
    python3 -m pytest scripts/test_extract.py -v
No local model / live API calls -- every heavy or networked call lives inside
functions these tests don't invoke."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import secrets_lib
import ocr_gemini


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
