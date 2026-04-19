from __future__ import annotations

from typing import Any

import pytest

from loopy_loop.config import ConfigError
from loopy_loop.config import load_root_config


def test_invalid_goal_slug_fails_fast(repo_builder: Any) -> None:
    repo_root = repo_builder(root_config={"goal_slug": "Bad Slug"})

    with pytest.raises(ConfigError, match="goal_slug"):
        load_root_config(repo_root=repo_root)
