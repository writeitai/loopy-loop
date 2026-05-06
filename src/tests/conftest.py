from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from loopy_loop.config import derive_goal_hash
from loopy_loop.models import CurrentTask
from loopy_loop.models import HistoryEntry
from loopy_loop.models import LoopState
from loopy_loop.models import RootConfigSnapshot
from loopy_loop.models import utc_now

GOAL = "Ship a minimal working landing page"


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def repo_builder(repo_root: Path):
    def build_repo(
        *,
        root_config: dict[str, Any] | None = None,
        workflows: dict[str, dict[str, Any]] | None = None,
    ) -> Path:
        config = {
            "goal_file": "loopy_loop_goal.txt",
            "completion_criteria": [
                "Homepage renders without errors",
                "Primary CTA is wired",
            ],
            "stop_criteria": ["A workflow writes an unresolvable error flag"],
            "max_turns": 20,
            "goal_check_consecutive_failures_cap": 3,
            "team_harness_provider": "openai_compat",
            "team_harness_model": "gpt-5.5",
            "team_harness_agents": ["codex"],
            "team_harness_agent_models": {},
            "team_harness_agent_reasoning_efforts": {},
            "team_harness_api_base": "https://openrouter.ai/api/v1",
            "team_harness_api_key_env": "OPENROUTER_API_KEY",
            "team_harness_system_prompt_extension": "",
        }
        if root_config is not None:
            config.update(root_config)
        repo_root.joinpath("loopy_loop_config.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )
        goal_file = config.get("goal_file")
        if isinstance(goal_file, str):
            goal_path = repo_root.joinpath(goal_file)
            goal_path.parent.mkdir(parents=True, exist_ok=True)
            goal_path.write_text(GOAL, encoding="utf-8")

        workflow_map = workflows or {
            "planner": {
                "prompt": "Plan the next repo change.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 0,
                    "description": "Plan work.",
                },
            },
            "goal_check": {
                "prompt": "Decide whether the goal is complete.",
                "config": {
                    "enabled": True,
                    "run_every": 1,
                    "must_follow": None,
                    "not_before_iteration": 1,
                    "description": "Check completion.",
                },
            },
        }
        workflows_dir = repo_root / ".loopy_loop" / "workflows"
        for workflow_id, workflow in workflow_map.items():
            workflow_dir = workflows_dir / workflow_id
            workflow_dir.mkdir(parents=True, exist_ok=True)
            workflow_dir.joinpath("prompt.txt").write_text(
                workflow["prompt"], encoding="utf-8"
            )
            workflow_dir.joinpath("config.yaml").write_text(
                yaml.safe_dump(workflow["config"], sort_keys=False), encoding="utf-8"
            )
        return repo_root

    return build_repo


@pytest.fixture()
def snapshot_factory():
    def factory(**overrides: Any) -> RootConfigSnapshot:
        data = {
            "goal": "Goal",
            "goal_hash": "cdbf6975e8a3",
            "completion_criteria": ["done"],
            "stop_criteria": ["blocked"],
            "max_turns": 20,
            "goal_check_consecutive_failures_cap": 3,
            "team_harness_provider": "openai_compat",
            "team_harness_model": "gpt-5.5",
            "team_harness_agents": ["codex"],
            "team_harness_agent_models": {},
            "team_harness_agent_reasoning_efforts": {},
            "team_harness_api_base": "https://openrouter.ai/api/v1",
            "team_harness_api_key_env": "OPENROUTER_API_KEY",
            "team_harness_system_prompt_extension": "",
        }
        data.update(overrides)
        if "goal_hash" not in overrides:
            data["goal_hash"] = derive_goal_hash(goal=data["goal"])
        return RootConfigSnapshot.model_validate(data)

    return factory


@pytest.fixture()
def history_entry_factory():
    def factory(**overrides: Any) -> HistoryEntry:
        now = utc_now()
        data = {
            "iteration": 1,
            "workflow_id": "planner",
            "session_id": "20260419_143022_cdbf6975e8a3_ab12cd34",
            "success": True,
            "error": None,
            "started_at": now - timedelta(minutes=1),
            "finished_at": now,
        }
        data.update(overrides)
        return HistoryEntry.model_validate(data)

    return factory


@pytest.fixture()
def current_task_factory():
    def factory(**overrides: Any) -> CurrentTask:
        data = {
            "workflow_id": "planner",
            "session_id": "20260419_143022_cdbf6975e8a3_ab12cd34",
            "iteration": 1,
            "started_at": utc_now(),
        }
        data.update(overrides)
        return CurrentTask.model_validate(data)

    return factory


@pytest.fixture()
def state_factory(snapshot_factory: Any):
    def factory(**overrides: Any) -> LoopState:
        snapshot = overrides.pop("config_snapshot", snapshot_factory())
        data = {
            "status": "running",
            "goal_hash": snapshot.goal_hash,
            "max_turns": snapshot.max_turns,
            "active_session_id": "20260419_143022_cdbf6975e8a3_ab12cd34",
            "goal_met": False,
            "stop_requested": False,
            "unresolvable_error": False,
            "stop_reason": None,
            "iteration_count": 0,
            "goal_check_consecutive_failures": 0,
            "current_task": None,
            "history": [],
            "config_snapshot": snapshot,
        }
        data.update(overrides)
        return LoopState.model_validate(data)

    return factory
