from __future__ import annotations

import asyncio
import json
from pathlib import Path
import traceback
from typing import Callable
from typing import Protocol

from team_harness import TeamHarness
from team_harness import TeamHarnessError
from team_harness import TeamHarnessResult

from loopy_loop.config import ConfigError
from loopy_loop.config import normalize_api_base
from loopy_loop.config import resolve_api_key
from loopy_loop.config import RootConfig
from loopy_loop.models import IterationResult
from loopy_loop.models import RootConfigSnapshot
from loopy_loop.sessions import HARNESS_RUN_ID_FILENAME
from loopy_loop.sessions import PROMPT_FILENAME
from loopy_loop.sessions import RESULT_FILENAME
from loopy_loop.sessions import RESULT_TEXT_FILENAME


class TeamHarnessLike(Protocol):
    async def run(self, task: str) -> TeamHarnessResult: ...


def run_harness_iteration(
    *,
    repo_root: Path,
    config_snapshot: RootConfigSnapshot,
    rendered_prompt: str,
    harness_factory: Callable[..., TeamHarnessLike] = TeamHarness,
) -> IterationResult:
    root_config = RootConfig.model_validate(config_snapshot.model_dump())
    resolved_api_key = resolve_api_key(config=root_config)
    harness = harness_factory(
        provider=config_snapshot.team_harness_provider,
        model=config_snapshot.team_harness_model,
        api_base=normalize_api_base(value=config_snapshot.team_harness_api_base),
        api_key=resolved_api_key,
        agents=config_snapshot.team_harness_agents,
        system_prompt=config_snapshot.team_harness_system_prompt_extension,
        cwd=str(repo_root),
        console_mode="silent",
    )
    try:
        result = asyncio.run(harness.run(task=rendered_prompt))
    except ConfigError:
        raise
    except TeamHarnessError as exc:
        traceback.print_exc()
        return IterationResult(
            success=False, text=None, error=str(exc), harness_run_id=""
        )
    except Exception as exc:
        traceback.print_exc()
        return IterationResult(
            success=False, text=None, error=str(exc), harness_run_id=""
        )
    return _normalize_harness_result(result=result)


def write_iteration_artifacts(
    *, iteration_dir: Path, rendered_prompt: str, iteration_result: IterationResult
) -> None:
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / PROMPT_FILENAME).write_text(rendered_prompt, encoding="utf-8")
    (iteration_dir / RESULT_TEXT_FILENAME).write_text(
        iteration_result.text or "", encoding="utf-8"
    )
    (iteration_dir / HARNESS_RUN_ID_FILENAME).write_text(
        iteration_result.harness_run_id, encoding="utf-8"
    )
    payload = iteration_result.model_dump()
    (iteration_dir / RESULT_FILENAME).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def _normalize_harness_result(*, result: TeamHarnessResult) -> IterationResult:
    return IterationResult(
        success=True, text=result.text, error=None, harness_run_id=result.run_id
    )
