from __future__ import annotations

from typing import Any

import pytest

from loopy_loop.config import ConfigError
from loopy_loop.config import derive_goal_hash
from loopy_loop.config import load_root_config
from loopy_loop.config import load_workflow_definitions
from loopy_loop.config import run_preflight


def test_load_root_config_and_workflows(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()

    root_config = load_root_config(repo_root=repo_root)
    workflows = load_workflow_definitions(repo_root=repo_root)
    preflight = run_preflight(repo_root=repo_root)

    assert root_config.team_harness_api_base == "https://openrouter.ai/api/v1"
    assert [workflow.id for workflow in workflows] == ["goal_check", "planner"]
    assert workflows[0].priority == 0
    assert workflows[0].run_on_start is False
    assert workflows[0].run_after_successes is None
    assert workflows[0].emits_goal_check is False
    assert preflight.root_config.goal_hash == derive_goal_hash(
        goal="Ship a minimal working landing page"
    )


def test_invalid_run_every_raises(repo_builder: Any) -> None:
    repo_root = repo_builder(
        workflows={
            "planner": {
                "prompt": "Prompt",
                "config": {
                    "enabled": True,
                    "run_every": 0,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            }
        }
    )

    with pytest.raises(ConfigError, match="run_every"):
        load_workflow_definitions(repo_root=repo_root)


def test_unresolved_must_follow_fails_preflight(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "implement": {
                "prompt": "Prompt",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": "planner",
                    "not_before_iteration": 0,
                    "description": "",
                },
            }
        }
    )

    with pytest.raises(ConfigError, match="must_follow references missing workflow"):
        run_preflight(repo_root=repo_root)


def test_unresolved_run_after_successes_fails_preflight(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(
        workflows={
            "reviewer": {
                "prompt": "Review",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "run_after_successes": {"workflow_id": "missing", "every": 10},
                    "description": "",
                },
            }
        }
    )

    with pytest.raises(
        ConfigError, match="run_after_successes references missing workflow"
    ):
        run_preflight(repo_root=repo_root)


def test_invalid_run_after_successes_every_raises(repo_builder: Any) -> None:
    repo_root = repo_builder(
        workflows={
            "reviewer": {
                "prompt": "Review",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "run_after_successes": {"workflow_id": "planner", "every": 0},
                    "description": "",
                },
            },
            "planner": {
                "prompt": "Plan",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                },
            },
        }
    )

    with pytest.raises(ConfigError, match="every"):
        load_workflow_definitions(repo_root=repo_root)


def test_missing_api_key_env_is_reported(repo_builder: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    repo_root = repo_builder()

    with pytest.raises(ConfigError, match="Missing required environment variable"):
        run_preflight(repo_root=repo_root)


def test_codex_provider_does_not_require_api_key_env(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    repo_root = repo_builder(root_config={"team_harness_provider": "codex"})

    preflight = run_preflight(repo_root=repo_root)

    assert preflight.root_config.team_harness_provider == "codex"


def test_unknown_root_config_field_rejected(repo_builder: Any) -> None:
    repo_root = repo_builder(root_config={"model": "typo-should-be-team-harness-model"})

    with pytest.raises(ConfigError, match="model"):
        load_root_config(repo_root=repo_root)


def test_unknown_workflow_config_field_rejected(repo_builder: Any) -> None:
    repo_root = repo_builder(
        workflows={
            "planner": {
                "prompt": "Prompt",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "",
                    "unknown_key": "value",
                },
            }
        }
    )

    with pytest.raises(ConfigError, match="unknown_key"):
        load_workflow_definitions(repo_root=repo_root)
