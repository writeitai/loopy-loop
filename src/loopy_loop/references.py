from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import re
from typing import Any

LOOPY_DIRNAME = ".loopy_loop"
SESSIONS_DIRNAME = "sessions"
CHILDREN_DIRNAME = "children"
SESSION_MANIFEST_FILENAME = "session.json"
PARENT_MANIFEST_FILENAME = "parent.json"
TRACES_DIRNAME = "traces"
TRACE_MANIFEST_FILENAME = "trace_manifest.json"

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
LOGICAL_REFERENCE_IMPLICIT_SCOPES = frozenset({"repo", "session", "root", "parent"})
LOGICAL_REFERENCE_NAMED_SCOPES = frozenset({"session", "trace"})
LOGICAL_REFERENCE_PATH_MARKER = ":/"
LOGICAL_REFERENCE_FORBIDDEN_CHARACTERS = frozenset({"\x00", "\\"})
LOGICAL_REFERENCE_INVALID_PATH_SEGMENTS = frozenset({"", ".", ".."})
LOGICAL_REFERENCE_ABSOLUTE_PATH_PREFIX = "/"


class LogicalReferenceError(ValueError):
    """A logical reference or the identity tree needed to resolve it is invalid."""


@dataclass(frozen=True)
class SessionIdentity:
    session_id: str
    directory: Path
    parent_session_id: str | None
    root_session_id: str
    depth: int


@dataclass(frozen=True)
class _SessionRecord:
    session_id: str
    directory: Path
    declared_parent_id: str | None
    physical_parent_id: str | None
    schema_version: int
    declared_root_id: str | None
    declared_depth: int | None


class LogicalReferenceResolver:
    """Resolve durable logical references for one validated session tree.

    The resolver validates topology before exposing any path. This deliberately
    avoids the ambiguous recursive basename search used by legacy path helpers.
    Returned paths are canonical absolute paths suitable for an attempt
    assignment; callers should continue to store the logical reference itself
    in durable records.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        current: SessionIdentity,
        sessions: Mapping[str, SessionIdentity],
        supplied_trace_roots: Mapping[str, Path],
    ) -> None:
        """Initialize a resolver from one already validated session tree."""

        self.repo_root = repo_root
        self.current = current
        self.sessions = dict(sessions)
        # Trace topology is intentionally independent from correctness state.
        # Do not inspect trace manifests while resolving session:/, root:/, or
        # parent:/ paths: a corrupt diagnostic trace must not make state/control
        # paths unavailable.  Trace discovery is performed only for trace: refs.
        self._supplied_trace_roots = dict(supplied_trace_roots)
        self._trace_roots: dict[str, Path] = {}
        self._resolved_trace_ids: set[str] = set()

    @classmethod
    def for_session(
        cls,
        *,
        repo_root: Path,
        session_id: str,
        trace_roots: Mapping[str, Path] | None = None,
    ) -> LogicalReferenceResolver:
        """Build a resolver after validating the requested session's tree."""

        _validate_id(value=session_id, label="session ID")
        root = _canonicalize(path=Path(repo_root), label="repository root")
        if not root.is_dir():
            raise LogicalReferenceError(f"repository root does not exist: {root}")

        sessions_root = root / LOOPY_DIRNAME / SESSIONS_DIRNAME
        resolved_sessions_root = _canonicalize(
            path=sessions_root, label="sessions root"
        )
        _require_within(
            path=resolved_sessions_root,
            root=root,
            label="sessions root",
            allow_equal=False,
        )
        identities = _validated_tree_containing_session(
            sessions_root=resolved_sessions_root, session_id=session_id
        )
        current = identities[session_id]

        same_tree = {
            identity.session_id: identity
            for identity in identities.values()
            if identity.root_session_id == current.root_session_id
        }
        return cls(
            repo_root=root,
            current=current,
            sessions=same_tree,
            supplied_trace_roots=trace_roots or {},
        )

    def _resolved_trace_root(self, *, identifier: str) -> Path:
        """Resolve one trace ID without validating unrelated diagnostic traces."""

        if identifier not in self._resolved_trace_ids:
            discovered = _trace_roots_for_tree(
                repo_root=self.repo_root,
                root_session_id=self.current.root_session_id,
                session_ids=frozenset(self.sessions),
                supplied=self._supplied_trace_roots,
                requested_identifier=identifier,
            )
            self._trace_roots.update(discovered)
            self._resolved_trace_ids.add(identifier)
        return self._trace_roots[identifier]

    def resolve(self, reference: str) -> Path:
        """Resolve one logical reference beneath its validated scope root."""

        scope, identifier, relative_parts = _parse_reference(reference=reference)
        if identifier is None:
            if scope == "repo":
                base = self.repo_root
            elif scope == "session":
                base = self.current.directory
            elif scope == "root":
                base = self.sessions[self.current.root_session_id].directory
            elif scope == "parent":
                parent_id = self.current.parent_session_id
                if parent_id is None:
                    raise LogicalReferenceError(
                        "parent:/ is undefined for a root session"
                    )
                base = self.sessions[parent_id].directory
            else:  # pragma: no cover - guarded by the parser
                raise AssertionError(f"unhandled logical-reference scope: {scope}")
        elif scope == "session":
            try:
                base = self.sessions[identifier].directory
            except KeyError as exc:
                raise LogicalReferenceError(
                    f"unknown session ID in logical reference: {identifier}"
                ) from exc
        elif scope == "trace":
            try:
                base = self._resolved_trace_root(identifier=identifier)
            except KeyError as exc:
                raise LogicalReferenceError(
                    f"unknown trace manifest ID in logical reference: {identifier}"
                ) from exc
        else:  # pragma: no cover - guarded by the parser
            raise AssertionError(f"unhandled named logical-reference scope: {scope}")

        return _resolve_beneath(base=base, parts=relative_parts, reference=reference)


def resolve_logical_reference(
    *,
    reference: str,
    repo_root: Path,
    session_id: str,
    trace_roots: Mapping[str, Path] | None = None,
) -> Path:
    """Validate the session tree and resolve one logical reference."""
    return LogicalReferenceResolver.for_session(
        repo_root=repo_root, session_id=session_id, trace_roots=trace_roots
    ).resolve(reference=reference)


def _scan_session_records(*, sessions_root: Path) -> dict[str, _SessionRecord]:
    """Scan every manifest-bearing session subtree below a sessions root."""

    if not sessions_root.is_dir():
        raise LogicalReferenceError(f"sessions root does not exist: {sessions_root}")

    records: dict[str, _SessionRecord] = {}
    for entry in _directory_entries(directory=sessions_root):
        if entry.is_symlink():
            raise LogicalReferenceError(
                f"session topology may not use a symlink: {entry}"
            )
        if not entry.is_dir():
            continue
        manifest = entry / SESSION_MANIFEST_FILENAME
        if manifest.is_symlink():
            raise LogicalReferenceError(
                f"session manifest may not be a symlink: {manifest}"
            )
        if not manifest.exists():
            continue
        _scan_session_subtree(
            directory=entry,
            physical_parent_id=None,
            sessions_root=sessions_root,
            records=records,
        )
    return records


def _validated_tree_containing_session(
    *, sessions_root: Path, session_id: str
) -> dict[str, SessionIdentity]:
    """Validate only the physical tree that can contain ``session_id``.

    Session roots are independent durable runs. A corrupt abandoned sibling
    must not make a healthy run's state references unavailable. We therefore
    locate candidate directories from the physical ``children/`` layout, then
    fully validate only candidate trees. Duplicate valid identities remain an
    error because choosing between them would be ambiguous.
    """

    candidate_roots = _candidate_tree_roots(
        sessions_root=sessions_root, session_id=session_id
    )
    valid: list[dict[str, SessionIdentity]] = []
    candidate_errors: list[str] = []
    for tree_root in candidate_roots:
        records: dict[str, _SessionRecord] = {}
        try:
            _scan_session_subtree(
                directory=tree_root,
                physical_parent_id=None,
                sessions_root=sessions_root,
                records=records,
            )
            identities = _validate_session_records(records=records)
        except LogicalReferenceError as exc:
            candidate_errors.append(str(exc))
            continue
        if session_id in identities:
            valid.append(identities)
    if len(valid) == 1:
        selected = valid[0]
        # Preserve the global no-duplicate-ID invariant across other healthy
        # trees, while deliberately ignoring unrelated corrupt trees.
        selected_root = selected[next(iter(selected))].directory
        while selected_root.parent != sessions_root:
            selected_root = selected_root.parent.parent
        for tree_root in _top_level_session_directories(sessions_root=sessions_root):
            if tree_root == selected_root:
                continue
            other_records: dict[str, _SessionRecord] = {}
            try:
                _scan_session_subtree(
                    directory=tree_root,
                    physical_parent_id=None,
                    sessions_root=sessions_root,
                    records=other_records,
                )
                other = _validate_session_records(records=other_records)
            except LogicalReferenceError:
                continue
            duplicates = sorted(set(selected) & set(other))
            if duplicates:
                raise LogicalReferenceError(
                    f"duplicate session ID {duplicates[0]!r} exists in multiple "
                    "session trees"
                )
        return selected
    if len(valid) > 1:
        raise LogicalReferenceError(
            f"duplicate session ID {session_id!r} exists in multiple session trees"
        )
    if candidate_errors:
        raise LogicalReferenceError(
            f"session {session_id!r} belongs to an invalid topology: "
            + "; ".join(candidate_errors)
        )
    raise LogicalReferenceError(f"unknown session ID: {session_id}")


def _candidate_tree_roots(*, sessions_root: Path, session_id: str) -> list[Path]:
    """Locate physical root trees that may contain the requested session ID."""

    roots: set[Path] = set()

    def walk(*, directory: Path, tree_root: Path) -> None:
        """Search one physical tree without following topology symlinks."""

        if directory.name == session_id:
            manifest = directory / SESSION_MANIFEST_FILENAME
            if manifest.is_symlink():
                raise LogicalReferenceError(
                    f"session manifest may not be a symlink: {manifest}"
                )
            if manifest.is_file():
                roots.add(tree_root)
        children = directory / CHILDREN_DIRNAME
        if children.is_symlink() or not children.is_dir():
            return
        try:
            entries = _directory_entries(directory=children)
        except LogicalReferenceError:
            return
        for child in entries:
            if child.name.startswith(".staging-"):
                continue
            if child.is_symlink():
                if child.name == session_id:
                    raise LogicalReferenceError(
                        f"session topology may not use a symlink: {child}"
                    )
                continue
            if child.is_dir():
                walk(directory=child, tree_root=tree_root)

    for entry in _directory_entries(directory=sessions_root):
        if entry.is_symlink():
            if entry.name == session_id:
                raise LogicalReferenceError(
                    f"session topology may not use a symlink: {entry}"
                )
            continue
        if not entry.is_dir():
            continue
        walk(directory=entry, tree_root=entry)
    return sorted(roots, key=lambda path: path.as_posix())


def _top_level_session_directories(*, sessions_root: Path) -> list[Path]:
    """List regular top-level session directories in deterministic order."""

    return [
        entry
        for entry in _directory_entries(directory=sessions_root)
        if not entry.is_symlink() and entry.is_dir()
    ]


def _scan_session_subtree(
    *,
    directory: Path,
    physical_parent_id: str | None,
    sessions_root: Path,
    records: dict[str, _SessionRecord],
) -> None:
    """Load one physical session subtree while ignoring non-session children."""

    if directory.is_symlink():
        raise LogicalReferenceError(
            f"session topology may not use a symlink: {directory}"
        )
    resolved_directory = _canonicalize(path=directory, label="session directory")
    _require_within(
        path=resolved_directory,
        root=sessions_root,
        label="session directory",
        allow_equal=False,
    )
    manifest_path = directory / SESSION_MANIFEST_FILENAME
    if manifest_path.is_symlink():
        raise LogicalReferenceError(
            f"session manifest may not be a symlink: {manifest_path}"
        )
    payload = _read_mapping(path=manifest_path, label="session manifest")
    session_id = _required_string(payload=payload, key="session_id", path=manifest_path)
    _validate_id(value=session_id, label="session ID")
    if directory.name != session_id:
        raise LogicalReferenceError(
            f"session manifest ID {session_id!r} does not match directory "
            f"{directory.name!r}"
        )
    if session_id in records:
        raise LogicalReferenceError(
            f"duplicate session ID {session_id!r}: "
            f"{records[session_id].directory} and {resolved_directory}"
        )

    schema_version_value = payload.get("schema_version", 1)
    if (
        isinstance(schema_version_value, bool)
        or not isinstance(schema_version_value, int)
        or schema_version_value < 1
    ):
        raise LogicalReferenceError(
            f"session manifest schema_version must be an integer: {manifest_path}"
        )
    parent_value = payload.get("parent_session_id")
    if parent_value is not None and not isinstance(parent_value, str):
        raise LogicalReferenceError(
            f"parent_session_id must be a string or null: {manifest_path}"
        )
    if isinstance(parent_value, str):
        _validate_id(value=parent_value, label="parent session ID")
    root_value = payload.get("root_session_id")
    if root_value is not None and not isinstance(root_value, str):
        raise LogicalReferenceError(
            f"root_session_id must be a string: {manifest_path}"
        )
    if isinstance(root_value, str):
        _validate_id(value=root_value, label="root session ID")
    depth_value = payload.get("depth")
    if depth_value is not None and (
        isinstance(depth_value, bool)
        or not isinstance(depth_value, int)
        or depth_value < 0
    ):
        raise LogicalReferenceError(
            f"depth must be a non-negative integer: {manifest_path}"
        )
    if schema_version_value >= 2 and (root_value is None or depth_value is None):
        raise LogicalReferenceError(
            f"v2 session manifest requires root_session_id and depth: {manifest_path}"
        )

    record = _SessionRecord(
        session_id=session_id,
        directory=resolved_directory,
        declared_parent_id=parent_value,
        physical_parent_id=physical_parent_id,
        schema_version=schema_version_value,
        declared_root_id=root_value,
        declared_depth=depth_value,
    )
    records[session_id] = record
    _validate_parent_manifest(record=record, sessions_root=sessions_root)

    children = directory / CHILDREN_DIRNAME
    if children.is_symlink():
        raise LogicalReferenceError(
            f"session children directory may not be a symlink: {children}"
        )
    if not children.exists():
        return
    if not children.is_dir():
        raise LogicalReferenceError(
            f"session children path is not a directory: {children}"
        )
    for child in _directory_entries(directory=children):
        if child.is_symlink():
            raise LogicalReferenceError(
                f"session topology may not use a symlink: {child}"
            )
        if not child.is_dir():
            continue
        # create_session_dir publishes immutable child identity from this
        # private sibling with an atomic rename.  A host crash may leave the
        # unpublished staging directory behind; it is not part of topology.
        if child.name.startswith(".staging-"):
            continue
        if not (child / SESSION_MANIFEST_FILENAME).is_file():
            # Agent work can accidentally leave ordinary directories below
            # the engine-owned children root.  Without an identity manifest
            # they are not topology and must not poison a healthy session.
            continue
        _scan_session_subtree(
            directory=child,
            physical_parent_id=session_id,
            sessions_root=sessions_root,
            records=records,
        )


def _validate_parent_manifest(*, record: _SessionRecord, sessions_root: Path) -> None:
    """Verify a child's optional parent manifest against physical topology."""

    path = record.directory / PARENT_MANIFEST_FILENAME
    if record.physical_parent_id is None:
        if path.exists() or path.is_symlink():
            raise LogicalReferenceError(
                f"root session must not contain parent.json: {record.directory}"
            )
        return
    if path.is_symlink():
        raise LogicalReferenceError(f"parent manifest may not be a symlink: {path}")
    if not path.exists():
        return
    payload = _read_mapping(path=path, label="parent manifest")
    parent_id = _required_string(payload=payload, key="parent_session_id", path=path)
    if parent_id != record.physical_parent_id:
        raise LogicalReferenceError(
            f"parent manifest contradicts session topology at {path}"
        )
    relative = _required_string(payload=payload, key="parent_relative_path", path=path)
    if "\x00" in relative or "\\" in relative or Path(relative).is_absolute():
        raise LogicalReferenceError(f"invalid parent_relative_path at {path}")
    resolved_parent = _canonicalize(
        path=record.directory / relative, label="parent manifest target"
    )
    _require_within(
        path=resolved_parent,
        root=sessions_root,
        label="parent manifest target",
        allow_equal=False,
    )
    expected_parent = _canonicalize(
        path=record.directory.parent.parent, label="physical parent session"
    )
    if resolved_parent != expected_parent:
        raise LogicalReferenceError(
            f"parent_relative_path does not identify the physical parent at {path}"
        )


def _validate_session_records(
    *, records: Mapping[str, _SessionRecord]
) -> dict[str, SessionIdentity]:
    """Validate parent/root/depth declarations and build session identities."""

    identities: dict[str, SessionIdentity] = {}
    for record in records.values():
        if record.declared_parent_id != record.physical_parent_id:
            raise LogicalReferenceError(
                f"session {record.session_id!r} declares parent "
                f"{record.declared_parent_id!r}, but its physical parent is "
                f"{record.physical_parent_id!r}"
            )
        if record.physical_parent_id is None:
            root_id = record.session_id
            depth = 0
        else:
            try:
                parent = identities[record.physical_parent_id]
            except KeyError as exc:
                raise LogicalReferenceError(
                    f"session {record.session_id!r} has an unknown parent "
                    f"{record.physical_parent_id!r}"
                ) from exc
            root_id = parent.root_session_id
            depth = parent.depth + 1

        if record.declared_root_id is not None and record.declared_root_id != root_id:
            raise LogicalReferenceError(
                f"session {record.session_id!r} declares root "
                f"{record.declared_root_id!r}, expected {root_id!r}"
            )
        if record.declared_depth is not None and record.declared_depth != depth:
            raise LogicalReferenceError(
                f"session {record.session_id!r} declares depth "
                f"{record.declared_depth}, expected {depth}"
            )
        identities[record.session_id] = SessionIdentity(
            session_id=record.session_id,
            directory=record.directory,
            parent_session_id=record.physical_parent_id,
            root_session_id=root_id,
            depth=depth,
        )
    return identities


def _trace_roots_for_tree(
    *,
    repo_root: Path,
    root_session_id: str,
    session_ids: frozenset[str],
    supplied: Mapping[str, Path],
    requested_identifier: str,
) -> dict[str, Path]:
    """Discover one requested trace identity without trusting unrelated traces."""

    traces_root = _canonicalize(
        path=repo_root / LOOPY_DIRNAME / TRACES_DIRNAME, label="traces root"
    )
    _require_within(
        path=traces_root, root=repo_root, label="traces root", allow_equal=False
    )
    result: dict[str, Path] = {}
    if traces_root.is_dir():
        for manifest_path in _walk_trace_manifests(traces_root=traces_root):
            try:
                payload = _read_mapping(path=manifest_path, label="trace manifest")
                identifier = _trace_manifest_id(payload=payload)
                if identifier is None:
                    continue
                _validate_id(value=identifier, label="trace manifest ID")
                if identifier != requested_identifier:
                    continue
                manifest_identity = payload.get("identity", {})
                if not isinstance(manifest_identity, dict):
                    raise LogicalReferenceError(
                        f"trace manifest identity must be an object: {manifest_path}"
                    )
            except LogicalReferenceError:
                # Trace data is diagnostic and independently repairable. An
                # invalid manifest cannot identify any trace, so skip it and
                # continue looking for the exact healthy identity requested.
                continue
            manifest_root_id = payload.get(
                "root_session_id", manifest_identity.get("root_session_id")
            )
            if manifest_root_id is not None and manifest_root_id != root_session_id:
                continue
            manifest_session_id = payload.get(
                "session_id", manifest_identity.get("session_id")
            )
            if (
                manifest_session_id is not None
                and manifest_session_id not in session_ids
            ):
                continue
            trace_root = _canonicalize(
                path=manifest_path.parent, label="trace manifest root"
            )
            relative = trace_root.relative_to(traces_root)
            if not relative.parts or relative.parts[0] != root_session_id:
                continue
            _register_trace_root(
                result=result, identifier=identifier, trace_root=trace_root
            )

    for identifier, raw_path in supplied.items():
        if identifier != requested_identifier:
            continue
        _validate_id(value=identifier, label="trace manifest ID")
        raw = Path(raw_path)
        trace_root = _canonicalize(
            path=raw.parent if raw.name == TRACE_MANIFEST_FILENAME else raw,
            label=f"trace root {identifier!r}",
        )
        _require_within(
            path=trace_root,
            root=traces_root,
            label=f"trace root {identifier!r}",
            allow_equal=False,
        )
        relative = trace_root.relative_to(traces_root)
        if not relative.parts or relative.parts[0] != root_session_id:
            raise LogicalReferenceError(
                f"trace root {identifier!r} does not belong to root session "
                f"{root_session_id!r}"
            )
        _register_trace_root(
            result=result, identifier=identifier, trace_root=trace_root
        )
    return result


def _walk_trace_manifests(*, traces_root: Path) -> list[Path]:
    """List regular trace manifests without following symlinked trace data."""

    manifests: list[Path] = []
    for directory, directory_names, file_names in os.walk(
        traces_root, followlinks=False
    ):
        current = Path(directory)
        directory_names[:] = sorted(
            name for name in directory_names if not (current / name).is_symlink()
        )
        if TRACE_MANIFEST_FILENAME in file_names:
            manifest = current / TRACE_MANIFEST_FILENAME
            if manifest.is_symlink():
                continue
            manifests.append(manifest)
    return sorted(manifests)


def _trace_manifest_id(*, payload: Mapping[str, Any]) -> str | None:
    """Extract the first supported logical identity from a trace manifest."""

    for key in ("trace_manifest_id", "manifest_id", "trace_id", "attempt_id"):
        value = payload.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise LogicalReferenceError(
                    f"trace manifest field {key} must be a string"
                )
            return value
    return None


def _register_trace_root(
    *, result: dict[str, Path], identifier: str, trace_root: Path
) -> None:
    """Register one unambiguous trace identity and canonical root."""

    existing = result.get(identifier)
    if existing is not None and existing != trace_root:
        raise LogicalReferenceError(
            f"duplicate trace manifest ID {identifier!r}: {existing} and {trace_root}"
        )
    result[identifier] = trace_root


def _parse_reference(*, reference: str) -> tuple[str, str | None, tuple[str, ...]]:
    """Parse and validate the durable logical-reference grammar."""

    if not reference:
        raise LogicalReferenceError("logical reference must be a non-empty string")
    if any(
        character in reference for character in LOGICAL_REFERENCE_FORBIDDEN_CHARACTERS
    ):
        raise LogicalReferenceError(
            f"logical reference contains a forbidden character: {reference!r}"
        )
    marker = LOGICAL_REFERENCE_PATH_MARKER
    if marker not in reference:
        raise LogicalReferenceError(
            f"logical reference does not match the required grammar: {reference!r}"
        )
    prefix, relative = reference.split(marker, 1)
    prefix_parts = prefix.split(":")
    if len(prefix_parts) == 1 and prefix_parts[0] in LOGICAL_REFERENCE_IMPLICIT_SCOPES:
        scope = prefix_parts[0]
        identifier = None
    elif (
        len(prefix_parts) == 2
        and prefix_parts[0] in LOGICAL_REFERENCE_NAMED_SCOPES
        and prefix_parts[1]
    ):
        scope, identifier = prefix_parts
        _validate_id(value=identifier, label=f"{scope} reference ID")
    else:
        raise LogicalReferenceError(
            f"logical reference has an unknown or malformed scope: {reference!r}"
        )
    if marker in relative or relative.startswith(
        LOGICAL_REFERENCE_ABSOLUTE_PATH_PREFIX
    ):
        raise LogicalReferenceError(f"malformed logical-reference path: {reference!r}")
    if relative == "":
        return scope, identifier, ()
    parts = tuple(relative.split("/"))
    if any(part in LOGICAL_REFERENCE_INVALID_PATH_SEGMENTS for part in parts):
        raise LogicalReferenceError(
            f"logical-reference path contains an invalid segment: {reference!r}"
        )
    return scope, identifier, parts


def _resolve_beneath(*, base: Path, parts: tuple[str, ...], reference: str) -> Path:
    """Resolve reference parts while enforcing containment beneath the scope."""

    canonical_base = _canonicalize(path=base, label="logical-reference root")
    _reject_symlink_loops(base=canonical_base, parts=parts, reference=reference)
    candidate = canonical_base.joinpath(*parts)
    resolved = _canonicalize(path=candidate, label=f"logical reference {reference!r}")
    _require_within(
        path=resolved,
        root=canonical_base,
        label=f"logical reference {reference!r}",
        allow_equal=True,
    )
    return resolved


def _reject_symlink_loops(
    *, base: Path, parts: tuple[str, ...], reference: str
) -> None:
    """Reject symlink cycles before canonical containment resolution."""

    current = base
    for part in parts:
        current /= part
        if not current.is_symlink():
            continue
        try:
            current.stat()
        except FileNotFoundError:
            # A dangling link still resolves to a lexical target. The ordinary
            # containment check below decides whether that target is safe.
            continue
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise LogicalReferenceError(
                    f"cannot resolve logical reference {reference!r}: symlink loop"
                ) from exc
            raise LogicalReferenceError(
                f"cannot inspect logical reference {reference!r} at {current}: {exc}"
            ) from exc


def _canonicalize(*, path: Path, label: str) -> Path:
    """Resolve a path without requiring its final component to exist."""

    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise LogicalReferenceError(f"cannot resolve {label} at {path}: {exc}") from exc


def _require_within(*, path: Path, root: Path, label: str, allow_equal: bool) -> None:
    """Require a canonical path to remain beneath its trusted root."""

    if (not allow_equal and path == root) or not path.is_relative_to(root):
        raise LogicalReferenceError(f"{label} escapes its validated root: {path}")


def _directory_entries(*, directory: Path) -> list[Path]:
    """List directory entries in byte-stable filename order."""

    try:
        return sorted(directory.iterdir(), key=lambda path: os.fsencode(path.name))
    except OSError as exc:
        raise LogicalReferenceError(
            f"cannot inspect directory {directory}: {exc}"
        ) from exc


def _read_mapping(*, path: Path, label: str) -> dict[str, Any]:
    """Read a JSON file and require a top-level object."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LogicalReferenceError(f"cannot read {label} at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LogicalReferenceError(f"{label} must be a JSON object: {path}")
    return payload


def _required_string(*, payload: Mapping[str, Any], key: str, path: Path) -> str:
    """Read one required non-empty string from a persisted object."""

    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise LogicalReferenceError(f"{key} must be a non-empty string at {path}")
    return value


def _validate_id(*, value: str, label: str) -> None:
    """Require an identifier that is safe for durable path and ref segments."""

    if not _SAFE_ID.fullmatch(value):
        raise LogicalReferenceError(f"invalid {label}: {value!r}")
