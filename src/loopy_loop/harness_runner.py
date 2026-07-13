from __future__ import annotations

import asyncio
from collections.abc import Iterable
import inspect
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
from loopy_loop.models import FailureKind
from loopy_loop.models import IterationResult
from loopy_loop.models import RootConfigSnapshot
from loopy_loop.sessions import HARNESS_RUN_ID_FILENAME
from loopy_loop.sessions import PROMPT_FILENAME
from loopy_loop.sessions import RESULT_FILENAME
from loopy_loop.sessions import RESULT_TEXT_FILENAME
from loopy_loop.sessions import write_json_atomic
from loopy_loop.sessions import write_text_atomic


class TeamHarnessLike(Protocol):
    async def run(self, task: str) -> TeamHarnessResult: ...


def run_harness_iteration(
    *,
    repo_root: Path,
    config_snapshot: RootConfigSnapshot,
    rendered_prompt: str,
    harness_output_root: Path | None = None,
    harness_factory: Callable[..., TeamHarnessLike] = TeamHarness,
) -> IterationResult:
    root_config = RootConfig.model_validate(
        config_snapshot.model_dump(exclude={"goal_hash"})
    )
    resolved_api_key = resolve_api_key(config=root_config)
    harness_kwargs = _build_harness_kwargs(
        repo_root=repo_root,
        config_snapshot=config_snapshot,
        resolved_api_key=resolved_api_key,
        harness_output_root=harness_output_root,
        harness_factory=harness_factory,
    )
    harness = harness_factory(**harness_kwargs)
    try:
        result = asyncio.run(harness.run(task=rendered_prompt))
    except ConfigError:
        raise
    except TeamHarnessError as exc:
        traceback.print_exc()
        harness_run_id, harness_output_dir = _failure_harness_paths(
            detail=exc.detail, harness_output_root=harness_output_root
        )
        return IterationResult(
            success=False,
            text=None,
            error=str(exc),
            error_detail=exc.detail,
            failure_kind=classify_failure_detail(detail=exc.detail),
            harness_run_id=harness_run_id,
            harness_output_dir=harness_output_dir,
        )
    except Exception as exc:
        traceback.print_exc()
        return IterationResult(
            success=False,
            text=None,
            error=str(exc),
            failure_kind="unknown",
            harness_run_id="",
        )
    return _normalize_harness_result(
        result=result, harness_output_root=harness_output_root
    )


def classify_failure_detail(*, detail: dict[str, object] | None) -> FailureKind:
    """Map team-harness failure detail onto the loopy failure taxonomy.

    team-harness coordinator failures carry a structured `retryable` bool
    (True for 429/5xx/network — already retried up to its max_retries;
    False for auth/other 4xx). Agent-process failure details carry no
    retryability signal, so they classify as "unknown".
    """
    if not detail:
        return "unknown"
    retryable = detail.get("retryable")
    if retryable is True:
        return "transient"
    if retryable is False:
        return "deterministic"
    return "unknown"


def _build_harness_kwargs(
    *,
    repo_root: Path,
    config_snapshot: RootConfigSnapshot,
    resolved_api_key: str | None,
    harness_output_root: Path | None,
    harness_factory: Callable[..., TeamHarnessLike],
) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "provider": config_snapshot.team_harness_provider,
        "model": config_snapshot.team_harness_model,
        "api_base": normalize_api_base(value=config_snapshot.team_harness_api_base),
        "api_key": resolved_api_key,
        "agents": config_snapshot.team_harness_agents,
        "system_prompt": config_snapshot.team_harness_system_prompt_extension,
        "cwd": str(repo_root),
        "console_mode": "silent",
    }
    retry_kwargs = {
        "max_retries": config_snapshot.team_harness_max_retries,
        "retry_base_delay_s": config_snapshot.team_harness_retry_base_delay_s,
        "retry_max_delay_s": config_snapshot.team_harness_retry_max_delay_s,
    }
    configured_retry_kwargs = {
        key: value for key, value in retry_kwargs.items() if value is not None
    }
    if configured_retry_kwargs:
        if _supports_kwargs(
            harness_factory=harness_factory, names=configured_retry_kwargs.keys()
        ):
            kwargs.update(configured_retry_kwargs)
        else:
            raise ConfigError(
                "Installed team-harness does not support coordinator retry "
                "controls; upgrade team-harness or remove "
                "team_harness_max_retries, team_harness_retry_base_delay_s, "
                "and team_harness_retry_max_delay_s from loopy_loop_config.yaml."
            )
    agent_override_kwargs = {
        "agent_models": config_snapshot.team_harness_agent_models,
        "agent_reasoning_efforts": config_snapshot.team_harness_agent_reasoning_efforts,
    }
    if _supports_kwargs(
        harness_factory=harness_factory, names=agent_override_kwargs.keys()
    ):
        kwargs.update(agent_override_kwargs)
    elif any(agent_override_kwargs.values()):
        raise ConfigError(
            "Installed team-harness does not support per-agent model overrides; "
            "upgrade team-harness or remove team_harness_agent_models and "
            "team_harness_agent_reasoning_efforts from loopy_loop_config.yaml."
        )
    if harness_output_root is not None:
        if _supports_kwargs(harness_factory=harness_factory, names=["output_dir"]):
            kwargs["output_dir"] = str(harness_output_root)
        else:
            raise ConfigError(
                "Installed team-harness does not support SDK output_dir; "
                "upgrade team-harness."
            )
    return kwargs


def _supports_kwargs(
    *, harness_factory: Callable[..., TeamHarnessLike], names: Iterable[str]
) -> bool:
    signature = inspect.signature(harness_factory)
    parameters = signature.parameters.values()
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return True
    return all(name in signature.parameters for name in names)


def _failure_harness_paths(
    *, detail: dict[str, object] | None, harness_output_root: Path | None
) -> tuple[str, str]:
    if not detail:
        return "", ""
    run_id_value = detail.get("run_id")
    output_dir_value = detail.get("session_output_dir")
    run_id = run_id_value if isinstance(run_id_value, str) else ""
    output_dir = output_dir_value if isinstance(output_dir_value, str) else ""
    if not output_dir and run_id and harness_output_root is not None:
        output_dir = str(harness_output_root / run_id)
    return run_id, output_dir


def write_iteration_artifacts(
    *, iteration_dir: Path, rendered_prompt: str, iteration_result: IterationResult
) -> None:
    # Atomic writes throughout: result.json is what post-crash recovery trusts
    # as proof of a completed iteration, and the rest should never exist
    # truncated either.
    iteration_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path=iteration_dir / PROMPT_FILENAME, content=rendered_prompt)
    write_text_atomic(
        path=iteration_dir / RESULT_TEXT_FILENAME, content=iteration_result.text or ""
    )
    write_text_atomic(
        path=iteration_dir / HARNESS_RUN_ID_FILENAME,
        content=iteration_result.harness_run_id,
    )
    write_json_atomic(
        path=iteration_dir / RESULT_FILENAME, payload=iteration_result.model_dump()
    )


def _normalize_harness_result(
    *, result: TeamHarnessResult, harness_output_root: Path | None = None
) -> IterationResult:
    harness_output_dir = ""
    if harness_output_root is not None and result.run_id:
        harness_output_dir = str(harness_output_root / result.run_id)
    return IterationResult(
        success=True,
        text=result.text,
        error=None,
        harness_run_id=result.run_id,
        harness_output_dir=harness_output_dir,
    )
