from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import UTC
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Literal
from urllib.parse import unquote
from urllib.parse import urlsplit

GIT_EVIDENCE_SCHEMA_VERSION = 2
DIRTY_TREE_ALGORITHM = "loopy-git-status-diff-v1-sha256"
ENGINE_RUNTIME_DIR = b".loopy_loop"
_ENGINE_RUNTIME_DIRECTORIES = {b"sessions", b"traces", b"trace_finalization_outbox"}
_ENGINE_RUNTIME_FILES = {b"repository.json", b"state.json", b"state.json.lock"}
_DIFF_PATHSPECS = (
    ".",
    ":(exclude).loopy_loop/sessions",
    ":(exclude).loopy_loop/sessions/**",
    ":(exclude).loopy_loop/traces",
    ":(exclude).loopy_loop/traces/**",
    ":(exclude).loopy_loop/trace_finalization_outbox",
    ":(exclude).loopy_loop/trace_finalization_outbox/**",
    ":(exclude).loopy_loop/repository.json",
    ":(exclude).loopy_loop/state.json",
    ":(exclude).loopy_loop/state.json.lock",
    ":(exclude).loopy_loop/state.json.archive_*",
)


class GitEvidenceError(RuntimeError):
    """Git evidence could not be captured without ambiguity."""


@dataclass(frozen=True)
class RemoteFingerprint:
    remote: str
    transport: str
    host: str | None
    fingerprint: str


@dataclass(frozen=True)
class DirtyTreeDigest:
    algorithm: str
    digest: str
    dirty: bool
    status_entry_count: int
    changed_path_count: int


@dataclass(frozen=True)
class GitEvidenceReceipt:
    schema_version: int
    phase: Literal["before", "after"]
    attempt_id: str | None
    captured_at: str
    repository_root: str
    branch: str | None
    detached: bool
    head: str | None
    dirty: bool
    dirty_tree_algorithm: str
    dirty_tree_digest: str
    status_entry_count: int
    changed_path_count: int
    remotes: tuple[RemoteFingerprint, ...]
    verbose_status_path: str | None = None
    verbose_diff_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible compact receipt."""
        return asdict(self)


@dataclass(frozen=True)
class _StatusEntry:
    status: bytes
    path: bytes
    source_path: bytes | None = None

    @property
    def paths(self) -> tuple[bytes, ...]:
        """Return every path named by this porcelain record."""
        if self.source_path is None:
            return (self.path,)
        return (self.path, self.source_path)

    def canonical_bytes(self) -> bytes:
        """Return the byte-stable representation used by the digest."""
        value = self.status + b" " + self.path
        if self.source_path is not None:
            value += b"\x00" + self.source_path
        return value


def capture_git_evidence(
    *,
    repo_root: Path,
    phase: Literal["before", "after"],
    attempt_id: str | None = None,
    verbose_status_path: Path | None = None,
    verbose_diff_path: Path | None = None,
) -> GitEvidenceReceipt:
    """Capture branch, HEAD, dirty status/diff, and credential-safe remotes."""
    if phase not in {"before", "after"}:
        raise GitEvidenceError(f"invalid git evidence phase: {phase!r}")
    root = _repository_root(repo_root=repo_root)
    status_entries = _filtered_status_entries(root=root)
    diff = _content_diff(root=root)
    digest = _digest_entries(entries=status_entries, diff=diff)

    branch_result = _git(
        root=root,
        arguments=("symbolic-ref", "--quiet", "--short", "HEAD"),
        allowed=(0, 1),
    )
    branch = (
        branch_result.stdout.decode("utf-8", "surrogateescape").strip()
        if branch_result.returncode == 0
        else None
    )
    head_result = _git(
        root=root, arguments=("rev-parse", "--verify", "HEAD"), allowed=(0, 128)
    )
    head = (
        head_result.stdout.decode("ascii").strip()
        if head_result.returncode == 0
        else None
    )

    status_output: str | None = None
    if verbose_status_path is not None:
        target = Path(verbose_status_path).resolve()
        _write_bytes_atomic(path=target, content=_render_status(entries=status_entries))
        status_output = str(target)
    diff_output: str | None = None
    if verbose_diff_path is not None:
        target = Path(verbose_diff_path).resolve()
        _write_bytes_atomic(path=target, content=diff)
        diff_output = str(target)

    return GitEvidenceReceipt(
        schema_version=GIT_EVIDENCE_SCHEMA_VERSION,
        phase=phase,
        attempt_id=attempt_id,
        captured_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        repository_root=str(root),
        branch=branch,
        detached=branch is None and head is not None,
        head=head,
        dirty=digest.dirty,
        dirty_tree_algorithm=digest.algorithm,
        dirty_tree_digest=digest.digest,
        status_entry_count=digest.status_entry_count,
        changed_path_count=digest.changed_path_count,
        remotes=sanitized_remote_fingerprints(repo_root=root),
        verbose_status_path=status_output,
        verbose_diff_path=diff_output,
    )


def dirty_tree_digest(*, repo_root: Path) -> DirtyTreeDigest:
    """Hash relevant porcelain status plus staged and unstaged Git diffs."""
    root = _repository_root(repo_root=repo_root)
    return _digest_entries(
        entries=_filtered_status_entries(root=root), diff=_content_diff(root=root)
    )


def sanitized_remote_fingerprints(*, repo_root: Path) -> tuple[RemoteFingerprint, ...]:
    """Fingerprint remotes without retaining URL userinfo, query, or path."""
    root = _repository_root(repo_root=repo_root)
    names_output = _git(root=root, arguments=("remote",)).stdout
    names = sorted(
        {
            line.decode("utf-8", "surrogateescape")
            for line in names_output.splitlines()
            if line
        }
    )
    result: list[RemoteFingerprint] = []
    for name in names:
        urls = _git(
            root=root, arguments=("remote", "get-url", "--all", name)
        ).stdout.splitlines()
        for raw_url in sorted(set(urls)):
            if not raw_url:
                continue
            transport, host, canonical = _sanitize_remote(raw_url=raw_url)
            result.append(
                RemoteFingerprint(
                    remote=name,
                    transport=transport,
                    host=host,
                    fingerprint=_sha256(value=canonical),
                )
            )
    return tuple(result)


def _repository_root(repo_root: Path) -> Path:
    """Return Git's canonical top-level directory for the requested path."""

    requested = Path(repo_root).resolve()
    inside = _git(
        root=requested, arguments=("rev-parse", "--is-inside-work-tree")
    ).stdout.strip()
    if inside != b"true":
        raise GitEvidenceError(f"not a Git working tree: {requested}")
    top_level = _git(
        root=requested, arguments=("rev-parse", "--show-toplevel")
    ).stdout.rstrip(b"\r\n")
    if not top_level:
        raise GitEvidenceError(f"Git returned no repository root for {requested}")
    return Path(os.fsdecode(top_level)).resolve()


def _filtered_status_entries(root: Path) -> tuple[_StatusEntry, ...]:
    """Return sorted porcelain entries excluding loopy runtime artifacts."""

    output = _git(
        root=root,
        arguments=(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=no",
        ),
    ).stdout
    entries = [
        entry
        for entry in _parse_status(output=output)
        if not all(_is_engine_path(path=path) for path in entry.paths)
    ]
    return tuple(sorted(entries, key=_StatusEntry.canonical_bytes))


def _parse_status(output: bytes) -> Iterator[_StatusEntry]:
    """Parse NUL-delimited porcelain-v1 records without decoding paths."""

    fields = output.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4 or field[2:3] != b" ":
            raise GitEvidenceError("Git returned malformed porcelain v1 status")
        status = field[:2]
        path = _normalize_git_path(path=field[3:])
        source_path: bytes | None = None
        if b"R" in status or b"C" in status:
            if index >= len(fields):
                raise GitEvidenceError("Git returned an incomplete rename status")
            source_path = _normalize_git_path(path=fields[index])
            index += 1
        yield _StatusEntry(status=status, path=path, source_path=source_path)


def _digest_entries(
    *, entries: tuple[_StatusEntry, ...], diff: bytes
) -> DirtyTreeDigest:
    """Bind status records and exact staged/unstaged diff bytes into a digest."""

    hasher = hashlib.sha256()
    _hash_field(hasher=hasher, value=DIRTY_TREE_ALGORITHM.encode("ascii"))
    paths: set[bytes] = set()
    for entry in entries:
        _hash_field(hasher=hasher, value=b"status")
        _hash_field(hasher=hasher, value=entry.canonical_bytes())
        paths.update(entry.paths)
    _hash_field(hasher=hasher, value=b"staged-and-unstaged-diff")
    _hash_field(hasher=hasher, value=diff)
    return DirtyTreeDigest(
        algorithm=DIRTY_TREE_ALGORITHM,
        digest="sha256:" + hasher.hexdigest(),
        dirty=bool(entries),
        status_entry_count=len(entries),
        changed_path_count=len(paths),
    )


def _content_diff(root: Path) -> bytes:
    """Return deterministic binary staged and unstaged diffs for relevant paths."""

    common = (
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "--",
        *_DIFF_PATHSPECS,
    )
    unstaged = _git(root=root, arguments=("diff", *common)).stdout
    staged = _git(root=root, arguments=("diff", "--cached", *common)).stdout
    return b"# unstaged\n" + unstaged + b"\n# staged\n" + staged


def _sanitize_remote(raw_url: bytes) -> tuple[str, str | None, bytes]:
    """Reduce one remote URL to transport, host, and credential-free identity."""

    value = raw_url.decode("utf-8", "surrogateescape").strip()
    value = value.split("?", 1)[0].split("#", 1)[0]
    if "://" in value:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower() or "unknown"
        try:
            port = parsed.port
        except ValueError as exc:
            raise GitEvidenceError("remote URL has an invalid port") from exc
        hostname = parsed.hostname.lower() if parsed.hostname else None
        path = unquote(parsed.path).rstrip("/")
        canonical = _remote_identity(hostname=hostname, port=port, path=path)
        return ("file" if scheme == "file" else scheme), hostname, canonical

    colon = value.find(":")
    slash = value.find("/")
    if colon > 0 and (slash == -1 or colon < slash):
        authority, path = value[:colon], value[colon + 1 :]
        hostname = authority.rsplit("@", 1)[-1].lower()
        return (
            "ssh",
            hostname,
            _remote_identity(hostname=hostname, port=None, path=path),
        )
    return "file", None, _remote_identity(hostname=None, port=None, path=value)


def _remote_identity(*, hostname: str | None, port: int | None, path: str) -> bytes:
    """Build the canonical remote identity bytes used for fingerprinting."""

    normalized_path = path.replace("\\", "/").strip("/")
    if normalized_path.endswith(".git"):
        normalized_path = normalized_path[:-4]
    return json.dumps(
        {"host": hostname or "", "port": port, "repository_path": normalized_path},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8", "surrogatepass")


def _render_status(*, entries: tuple[_StatusEntry, ...]) -> bytes:
    """Render status records as deterministic newline-delimited JSON bytes."""

    lines = []
    for entry in entries:
        item: dict[str, str] = {
            "status": entry.status.decode("ascii", "replace"),
            "path": entry.path.decode("utf-8", "surrogateescape"),
        }
        if entry.source_path is not None:
            item["source_path"] = entry.source_path.decode("utf-8", "surrogateescape")
        lines.append(json.dumps(item, sort_keys=True, ensure_ascii=True))
    suffix = "\n" if lines else ""
    return ("\n".join(lines) + suffix).encode("utf-8")


def _write_bytes_atomic(*, path: Path, content: bytes) -> None:
    """Atomically replace a file with the supplied byte content."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _git(
    *, root: Path, arguments: tuple[str, ...], allowed: tuple[int, ...] = (0,)
) -> subprocess.CompletedProcess[bytes]:
    """Run one Git command and accept only the explicitly allowed exit codes."""

    try:
        result = subprocess.run(
            ("git", "-C", os.fspath(root), *arguments), check=False, capture_output=True
        )
    except OSError as exc:
        raise GitEvidenceError(f"cannot execute Git: {exc}") from exc
    if result.returncode not in allowed:
        detail = result.stderr.decode("utf-8", "replace").strip()
        command = "git " + " ".join(arguments[:3])
        raise GitEvidenceError(
            f"{command} failed with exit code {result.returncode}: {detail}"
        )
    return result


def _normalize_git_path(path: bytes) -> bytes:
    """Normalize Git's trailing slash for an untracked repository boundary."""
    normalized = path[:-1] if path.endswith(b"/") else path
    if (
        not normalized
        or normalized.startswith(b"/")
        or b"\x00" in normalized
        or any(part in {b"", b".", b".."} for part in normalized.split(b"/"))
    ):
        raise GitEvidenceError("Git returned an unsafe working-tree path")
    return normalized


def _is_engine_path(*, path: bytes) -> bool:
    """Return whether a Git path names coordinator-owned runtime state."""

    prefix = ENGINE_RUNTIME_DIR + b"/"
    if not path.startswith(prefix):
        return False
    relative = path[len(prefix) :]
    first = relative.split(b"/", 1)[0]
    return (
        first in _ENGINE_RUNTIME_DIRECTORIES
        or relative in _ENGINE_RUNTIME_FILES
        or relative.startswith(b"state.json.archive_")
    )


def _hash_field(*, hasher: object, value: bytes) -> None:
    """Add one length-delimited field to the dirty-tree digest."""

    assert isinstance(hasher, type(hashlib.sha256()))
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _sha256(*, value: bytes) -> str:
    """Return a prefixed SHA-256 digest for canonical evidence bytes."""

    return "sha256:" + hashlib.sha256(value).hexdigest()
