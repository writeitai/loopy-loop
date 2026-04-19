from __future__ import annotations

import json
import re
from typing import Any

from loopy_loop.sessions import create_session_dir
from loopy_loop.sessions import create_session_id
from loopy_loop.sessions import ensure_iteration_dir


def test_session_id_format() -> None:
    session_id = create_session_id(goal_slug="ship-landing-page")

    assert re.fullmatch(r"ship-landing-page_\d{8}_\d{6}_[a-f0-9]{8}", session_id)


def test_create_session_and_iteration_dirs(repo_root: Any) -> None:
    session_dir = create_session_dir(
        repo_root=repo_root,
        session_id="ship-landing-page_20260419_143022_ab12cd34",
        goal_slug="ship-landing-page",
    )
    iteration_dir = ensure_iteration_dir(
        repo_root=repo_root,
        session_id="ship-landing-page_20260419_143022_ab12cd34",
        iteration=1,
        workflow_id="planner",
    )

    metadata = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))

    assert metadata["session_id"] == "ship-landing-page_20260419_143022_ab12cd34"
    assert (session_dir / "events.jsonl").exists()
    assert iteration_dir.name == "0001_planner"
