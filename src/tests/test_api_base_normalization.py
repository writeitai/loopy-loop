from __future__ import annotations

from loopy_loop.config import normalize_api_base


def test_normalize_api_base_appends_v1_when_missing() -> None:
    assert normalize_api_base(value="https://openrouter.ai/api") == (
        "https://openrouter.ai/api/v1"
    )


def test_normalize_api_base_strips_trailing_slash() -> None:
    assert normalize_api_base(value="https://openrouter.ai/api/v1/") == (
        "https://openrouter.ai/api/v1"
    )
