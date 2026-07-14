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
    workflows = load_workflow_definitions(repo_root=repo_root, workflow_set="main")
    preflight = run_preflight(repo_root=repo_root)

    assert root_config.team_harness_api_base == "https://openrouter.ai/api/v1"
    assert root_config.team_harness_agent_models == {}
    assert root_config.team_harness_agent_reasoning_efforts == {}
    assert root_config.team_harness_max_retries is None
    assert root_config.team_harness_retry_base_delay_s is None
    assert root_config.team_harness_retry_max_delay_s is None
    assert root_config.goal == "Ship a minimal working landing page"
    assert [workflow.id for workflow in workflows] == ["goal_check", "planner"]
    assert workflows[0].workflow_set == "main"
    assert workflows[0].priority == 0
    assert workflows[0].run_on_start is False
    assert workflows[0].run_after_successes is None
    assert workflows[0].emits_goal_check is False
    assert preflight.root_config.goal_hash == derive_goal_hash(
        goal="Ship a minimal working landing page"
    )
    assert preflight.workflow_set == "main"


def test_preflight_uses_configured_workflow_set(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(root_config={"workflow_set": "pm_planner_dispatcher"})

    preflight = run_preflight(repo_root=repo_root)

    assert preflight.workflow_set == "pm_planner_dispatcher"
    assert {workflow.workflow_set for workflow in preflight.workflows} == {
        "pm_planner_dispatcher"
    }


def test_preflight_override_updates_config_snapshot_workflow_set(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(root_config={"workflow_set": "main"})
    workflow_dir = (
        repo_root / ".loopy_loop" / "workflow_sets" / "other" / "workflows" / "work"
    )
    workflow_dir.mkdir(parents=True)
    workflow_dir.joinpath("prompt.txt").write_text("Work", encoding="utf-8")
    workflow_dir.joinpath("config.yaml").write_text(
        "enabled: true\nrun_every: 1\n", encoding="utf-8"
    )

    preflight = run_preflight(repo_root=repo_root, workflow_set="other")

    assert preflight.workflow_set == "other"
    assert preflight.root_config.workflow_set == "other"


def test_legacy_workflows_directory_is_not_loaded(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    legacy = repo_root / ".loopy_loop" / "workflows" / "legacy"
    legacy.mkdir(parents=True)
    legacy.joinpath("prompt.txt").write_text("Legacy prompt", encoding="utf-8")
    legacy.joinpath("config.yaml").write_text(
        "enabled: true\nrun_every: 1\n", encoding="utf-8"
    )

    workflows = load_workflow_definitions(repo_root=repo_root, workflow_set="main")

    assert "legacy" not in {workflow.id for workflow in workflows}


def test_preflight_reports_legacy_workflows_directory(
    repo_builder: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder()
    config_path = repo_root / "loopy_loop_config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "workflow_set: main", "workflow_set: missing"
        ),
        encoding="utf-8",
    )
    legacy = repo_root / ".loopy_loop" / "workflows" / "legacy"
    legacy.mkdir(parents=True)
    legacy.joinpath("prompt.txt").write_text("Legacy prompt", encoding="utf-8")
    legacy.joinpath("config.yaml").write_text(
        "enabled: true\nrun_every: 1\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="Legacy workflow directory"):
        run_preflight(repo_root=repo_root)


def test_load_root_config_uses_goal_file_and_optional_defaults(
    repo_builder: Any,
) -> None:
    repo_root = repo_builder()

    repo_root.joinpath("loopy_loop_config.yaml").write_text(
        "\n".join(
            [
                'goal_file: "custom_goal.txt"',
                'workflow_set: "main"',
                "max_turns: 20",
                'team_harness_provider: "codex"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    repo_root.joinpath("custom_goal.txt").write_text(
        "Build the simplest useful thing.\n", encoding="utf-8"
    )

    root_config = load_root_config(repo_root=repo_root)

    assert root_config.goal == "Build the simplest useful thing."
    assert root_config.workflow_set == "main"
    assert root_config.completion_criteria == []
    assert root_config.stop_criteria == []
    assert root_config.team_harness_system_prompt_extension == ""


def test_load_root_config_accepts_team_harness_retry_controls(
    repo_builder: Any,
) -> None:
    repo_root = repo_builder(
        root_config={
            "team_harness_max_retries": 8,
            "team_harness_retry_base_delay_s": 2.0,
            "team_harness_retry_max_delay_s": 60.0,
        }
    )

    root_config = load_root_config(repo_root=repo_root)

    assert root_config.team_harness_max_retries == 8
    assert root_config.team_harness_retry_base_delay_s == 2.0
    assert root_config.team_harness_retry_max_delay_s == 60.0


def test_load_root_config_rejects_invalid_retry_delay_bounds(repo_builder: Any) -> None:
    repo_root = repo_builder(
        root_config={
            "team_harness_retry_base_delay_s": 10.0,
            "team_harness_retry_max_delay_s": 1.0,
        }
    )

    with pytest.raises(ConfigError, match="team_harness_retry_max_delay_s"):
        load_root_config(repo_root=repo_root)


def test_load_root_config_rejects_inline_goal(repo_builder: Any) -> None:
    repo_root = repo_builder(root_config={"goal": "Inline goals are no longer used"})

    with pytest.raises(ConfigError, match="use 'goal_file' instead"):
        load_root_config(repo_root=repo_root)


def test_load_root_config_rejects_missing_goal_file(repo_builder: Any) -> None:
    repo_root = repo_builder(root_config={"goal_file": "missing_goal.txt"})
    repo_root.joinpath("missing_goal.txt").unlink()

    with pytest.raises(ConfigError, match="Unable to read goal file"):
        load_root_config(repo_root=repo_root)


def test_load_root_config_rejects_empty_goal_file(repo_builder: Any) -> None:
    repo_root = repo_builder(root_config={"goal_file": "empty_goal.txt"})
    repo_root.joinpath("empty_goal.txt").write_text("  \n", encoding="utf-8")

    with pytest.raises(ConfigError, match="must not be empty"):
        load_root_config(repo_root=repo_root)


def test_load_root_config_requires_workflow_set(repo_builder: Any) -> None:
    repo_root = repo_builder()
    repo_root.joinpath("loopy_loop_config.yaml").write_text(
        "\n".join(
            [
                'goal_file: "loopy_loop_goal.txt"',
                "max_turns: 20",
                'team_harness_provider: "codex"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="workflow_set"):
        load_root_config(repo_root=repo_root)


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
        load_workflow_definitions(repo_root=repo_root, workflow_set="main")


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
        load_workflow_definitions(repo_root=repo_root, workflow_set="main")


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


def test_agent_model_maps_allow_non_empty_string_values(repo_builder: Any) -> None:
    repo_root = repo_builder(
        root_config={
            "team_harness_agent_models": {"codex": "gpt-5.5"},
            "team_harness_agent_reasoning_efforts": {"codex": "high"},
        }
    )

    root_config = load_root_config(repo_root=repo_root)

    assert root_config.team_harness_agent_models == {"codex": "gpt-5.5"}
    assert root_config.team_harness_agent_reasoning_efforts == {"codex": "high"}


def test_agent_model_maps_reject_empty_values(repo_builder: Any) -> None:
    repo_root = repo_builder(root_config={"team_harness_agent_models": {"codex": ""}})

    with pytest.raises(ConfigError, match="mapping value"):
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
        load_workflow_definitions(repo_root=repo_root, workflow_set="main")


_TIER_CONFIG: dict[str, Any] = {
    "team_harness_agents": ["codex", "claude"],
    "model_tiers": {
        "strong": {
            "codex": {"model": "gpt-5.6-sol", "effort": "high"},
            "claude": {"model": "claude-fable-5"},
        },
        "economy": {
            "codex": {"model": "gpt-5.6-terra", "effort": "low"},
            "claude": {"model": "claude-haiku-4-5", "effort": "medium"},
        },
    },
}


def test_default_tier_derives_agent_models_and_efforts(repo_builder: Any) -> None:
    repo_root = repo_builder(root_config={**_TIER_CONFIG, "default_tier": "economy"})

    root_config = load_root_config(repo_root=repo_root)

    assert root_config.team_harness_agent_models == {
        "codex": "gpt-5.6-terra",
        "claude": "claude-haiku-4-5",
    }
    assert root_config.team_harness_agent_reasoning_efforts == {
        "codex": "low",
        "claude": "medium",
    }


def test_model_tiers_render_guidance_into_prompt_extension(repo_builder: Any) -> None:
    repo_root = repo_builder(root_config={**_TIER_CONFIG, "default_tier": "economy"})

    root_config = load_root_config(repo_root=repo_root)
    extension = root_config.team_harness_system_prompt_extension

    assert "Model tier policy:" in extension
    assert "- strong: codex model=gpt-5.6-sol effort=high; " in extension
    assert "claude model=claude-fable-5" in extension
    assert "Default tier: economy" in extension


def test_model_tiers_guidance_appends_after_existing_extension(
    repo_builder: Any,
) -> None:
    repo_root = repo_builder(
        root_config={
            **_TIER_CONFIG,
            "team_harness_system_prompt_extension": "House rule: keep PRs small.",
        }
    )

    root_config = load_root_config(repo_root=repo_root)
    extension = root_config.team_harness_system_prompt_extension

    assert extension.startswith("House rule: keep PRs small.")
    assert "Model tier policy:" in extension


def test_model_tiers_without_default_tier_keep_agent_models_unchanged(
    repo_builder: Any,
) -> None:
    repo_root = repo_builder(root_config=dict(_TIER_CONFIG))

    root_config = load_root_config(repo_root=repo_root)

    assert root_config.team_harness_agent_models == {}
    assert root_config.team_harness_agent_reasoning_efforts == {}
    assert "Model tier policy:" in root_config.team_harness_system_prompt_extension
    assert "Default tier" not in root_config.team_harness_system_prompt_extension


def test_unknown_default_tier_rejected(repo_builder: Any) -> None:
    repo_root = repo_builder(root_config={**_TIER_CONFIG, "default_tier": "turbo"})

    with pytest.raises(ConfigError, match="default_tier 'turbo'"):
        load_root_config(repo_root=repo_root)


def test_default_tier_conflicts_with_explicit_agent_models(repo_builder: Any) -> None:
    repo_root = repo_builder(
        root_config={
            **_TIER_CONFIG,
            "default_tier": "economy",
            "team_harness_agent_models": {"codex": "gpt-5.5"},
        }
    )

    with pytest.raises(ConfigError, match="one source of truth"):
        load_root_config(repo_root=repo_root)


def test_model_tiers_reject_agents_missing_from_team_harness_agents(
    repo_builder: Any,
) -> None:
    repo_root = repo_builder(
        root_config={
            "team_harness_agents": ["codex"],
            "model_tiers": {"strong": {"gemini": {"model": "gemini-3.5-pro"}}},
        }
    )

    with pytest.raises(ConfigError, match="missing from"):
        load_root_config(repo_root=repo_root)


def test_model_tiers_reject_empty_tier_and_empty_model(repo_builder: Any) -> None:
    repo_root = repo_builder(root_config={"model_tiers": {"strong": {}}})
    with pytest.raises(ConfigError, match="must not be empty"):
        load_root_config(repo_root=repo_root)

    repo_root = repo_builder(
        root_config={"model_tiers": {"strong": {"codex": {"model": "  "}}}}
    )
    with pytest.raises(ConfigError, match="model must not be empty"):
        load_root_config(repo_root=repo_root)


def test_model_tiers_stay_out_of_wire_snapshot(repo_builder: Any) -> None:
    from loopy_loop.coordinator_app import _COORDINATOR_ONLY_FIELDS
    from loopy_loop.models import RootConfigSnapshot

    repo_root = repo_builder(root_config={**_TIER_CONFIG, "default_tier": "economy"})
    root_config = load_root_config(repo_root=repo_root)

    snapshot = RootConfigSnapshot.model_validate(
        root_config.model_dump(exclude=_COORDINATOR_ONLY_FIELDS)
    )

    assert snapshot.team_harness_agent_models == {
        "codex": "gpt-5.6-terra",
        "claude": "claude-haiku-4-5",
    }
    assert "Model tier policy:" in snapshot.team_harness_system_prompt_extension
    assert "model_tiers" not in snapshot.model_dump()


def test_run_preflight_accepts_resolved_default_tier(
    repo_builder: Any, monkeypatch: Any
) -> None:
    """Regression: PreflightResult re-validates its nested RootConfig, so the
    resolved config (default_tier + DERIVED mappings) must satisfy its own
    validators."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    repo_root = repo_builder(root_config={**_TIER_CONFIG, "default_tier": "economy"})

    preflight = run_preflight(repo_root=repo_root)

    assert preflight.root_config.team_harness_agent_models == {
        "codex": "gpt-5.6-terra",
        "claude": "claude-haiku-4-5",
    }


def test_default_tier_must_cover_all_agents(repo_builder: Any) -> None:
    repo_root = repo_builder(
        root_config={
            "team_harness_agents": ["codex", "claude", "gemini"],
            "model_tiers": {"economy": {"codex": {"model": "gpt-5.6-terra"}}},
            "default_tier": "economy",
        }
    )

    with pytest.raises(ConfigError, match="missing: \\['claude', 'gemini'\\]"):
        load_root_config(repo_root=repo_root)


def test_model_tiers_reject_multiline_values(repo_builder: Any) -> None:
    repo_root = repo_builder(
        root_config={
            "model_tiers": {
                "strong": {"codex": {"model": "gpt-5.6-sol\n- injected: bullet"}}
            }
        }
    )

    with pytest.raises(ConfigError, match="single line"):
        load_root_config(repo_root=repo_root)


def test_model_tiers_reject_wrong_shape(repo_builder: Any) -> None:
    repo_root = repo_builder(
        root_config={"model_tiers": {"strong": {"codex": "gpt-5.6-sol"}}}
    )

    with pytest.raises(ConfigError):
        load_root_config(repo_root=repo_root)
