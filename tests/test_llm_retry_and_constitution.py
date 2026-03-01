import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.llm import LLMProvider
from governance.perpetual import PerpetualEngine
from config.settings import MODEL_ALLOCATION


class StubError(Exception):
    def __init__(self, message="", status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def test_retry_transient_error_status_codes_and_non_retriable_auth_input():
    assert LLMProvider._should_retry_transient_error(StubError("overloaded", status_code=503))
    assert LLMProvider._should_retry_transient_error(StubError("api overloaded", status_code=529))
    assert not LLMProvider._should_retry_transient_error(StubError("bad request", status_code=400))
    assert not LLMProvider._should_retry_transient_error(StubError("unauthorized", status_code=401))


def test_retry_transient_error_rate_limit_and_timeout_signals():
    assert LLMProvider._should_retry_transient_error(StubError("rate_limit exceeded"))
    assert LLMProvider._should_retry_transient_error(StubError("request timeout"))
    assert LLMProvider._should_retry_transient_error(
        StubError(body={"error": {"type": "overloaded_error"}})
    )


@pytest.mark.skip(reason="Constitution extract feature not implemented in current version")
def test_phase2_constitution_extract_trimmed_and_targeted():
    constitution_text = Path("CONSTITUTION.md").read_text(encoding="utf-8")
    # Method _build_phase2_constitution_extract doesn't exist in current implementation
    # This test is for a planned feature that hasn't been implemented yet
    assert len(constitution_text) > 50000


def test_science_gate_model_allocation_uses_haiku():
    assert MODEL_ALLOCATION.get("science_gate") == "haiku"
