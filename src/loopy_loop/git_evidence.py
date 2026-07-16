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
import stat
import subprocess
import tempfile
from typing import Literal
from urllib.parse import unquote
from urllib.parse import urlsplit

GIT_EVIDENCE_SCHEMA_VERSION = 2
DIRTY_TREE_ALGORITHM = "loopy-dirty-tree-v2-sha256"
ENGINE_RUNTIME_DIR = b".loopy_loop"
_ENGINE_RUNTIME_DIRECTORIES = {
    b"sessions",
    b"traces",
    b"trace_export_outbox",
    b"trace_finalization_outbox",
}
_ENGINE_RUNTIME_FILES = {b"repository.json", b"state.json", b"state.json.lock"}


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
        if self.source_path is None:
            return (self.path,)
        return (self.path, self.source_path)

    def canonical_bytes(self) -> bytes:
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
    """Capture compact, credential-safe facts for one attempt boundary."""
    if phase not in {"before", "after"}:
        raise GitEvidenceError(f"invalid git evidence phase: {phase!r}")
    root = _repository_root(repo_root)
    status_entries = _filtered_status_entries(root)
    digest = _digest_entries(root=root, entries=status_entries)

    branch_result = _git(
        root, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1)
    )
    branch = (
        branch_result.stdout.decode("utf-8", "surrogateescape").strip()
        if branch_result.returncode == 0
        else None
    )
    head_result = _git(root, "rev-parse", "--verify", "HEAD", allowed=(0, 128))
    head = (
        head_result.stdout.decode("ascii").strip()
        if head_result.returncode == 0
        else None
    )

    status_output: str | None = None
    if verbose_status_path is not None:
        target = Path(verbose_status_path).resolve()
        _write_bytes_atomic(target, _render_status(status_entries))
        status_output = str(target)
    diff_output: str | None = None
    if verbose_diff_path is not None:
        target = Path(verbose_diff_path).resolve()
        _write_bytes_atomic(target, _verbose_diff(root))
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
    """Return the canonical digest for relevant working-tree changes."""
    root = _repository_root(repo_root)
    return _digest_entries(root=root, entries=_filtered_status_entries(root))


def sanitized_remote_fingerprints(*, repo_root: Path) -> tuple[RemoteFingerprint, ...]:
    """Fingerprint remotes without retaining URL userinfo, query, or path."""
    root = _repository_root(repo_root)
    names_output = _git(root, "remote").stdout
    names = sorted(
        {
            line.decode("utf-8", "surrogateescape")
            for line in names_output.splitlines()
            if line
        }
    )
    result: list[RemoteFingerprint] = []
    for name in names:
        urls = _git(root, "remote", "get-url", "--all", name).stdout.splitlines()
        for raw_url in sorted(set(urls)):
            if not raw_url:
                continue
            transport, host, canonical = _sanitize_remote(raw_url)
            result.append(
                RemoteFingerprint(
                    remote=name,
                    transport=transport,
                    host=host,
                    fingerprint=_sha256(canonical),
                )
            )
    return tuple(result)


def _repository_root(repo_root: Path) -> Path:
    requested = Path(repo_root).resolve()
    try:
        inside = _git(requested, "rev-parse", "--is-inside-work-tree").stdout.strip()
        if inside != b"true":
            raise GitEvidenceError(f"not a Git working tree: {requested}")
        top_level = _git(requested, "rev-parse", "--show-toplevel").stdout.rstrip(
            b"\r\n"
        )
    except GitEvidenceError:
        raise
    except OSError as exc:  # pragma: no cover - Path.resolve normally catches nothing
        raise GitEvidenceError(f"cannot inspect repository {requested}: {exc}") from exc
    if not top_level:
        raise GitEvidenceError(f"Git returned no repository root for {requested}")
    return Path(os.fsdecode(top_level)).resolve()


def _filtered_status_entries(root: Path) -> tuple[_StatusEntry, ...]:
    output = _git(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no"
    ).stdout
    entries = []
    for entry in _parse_status(output):
        if all(_is_engine_path(path) for path in entry.paths):
            continue
        entries.append(entry)
    # The algorithm contract sorts the complete porcelain records bytewise,
    # independently of locale and Git's presentation order.
    return tuple(sorted(entries, key=_StatusEntry.canonical_bytes))


def _parse_status(output: bytes) -> Iterator[_StatusEntry]:
    fields = output.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4 or field[2:3] != b" ":
            raise GitEvidenceError("Git returned malformed porcelain v1 status")
        status_value = field[:2]
        path = _normalize_git_path(field[3:])
        source: bytes | None = None
        if b"R" in status_value or b"C" in status_value:
            if index >= len(fields):
                raise GitEvidenceError("Git returned an incomplete rename status")
            source = _normalize_git_path(fields[index])
            index += 1
        yield _StatusEntry(status=status_value, path=path, source_path=source)


def _digest_entries(
    *, root: Path, entries: tuple[_StatusEntry, ...]
) -> DirtyTreeDigest:
    hasher = hashlib.sha256()
    _hash_field(hasher, DIRTY_TREE_ALGORITHM.encode("ascii"))
    # Bind the exact staged/index subject as well as porcelain status and
    # working-tree bytes. Without this, two partial-staging states can have
    # identical HEAD/status/worktree while selecting different staged blobs.
    for record in _index_records(root):
        _hash_field(hasher, b"index-entry")
        _hash_field(hasher, record)
    relevant_paths: set[bytes] = set()
    for entry in entries:
        _hash_field(hasher, b"status")
        _hash_field(hasher, entry.status)
        _hash_field(hasher, b"path")
        _hash_field(hasher, entry.path)
        if not _is_engine_path(entry.path):
            relevant_paths.add(entry.path)
        if entry.source_path is not None:
            _hash_field(hasher, b"source")
            _hash_field(hasher, entry.source_path)
            if not _is_engine_path(entry.source_path):
                relevant_paths.add(entry.source_path)

    for path in sorted(relevant_paths):
        _hash_field(hasher, b"file-fact")
        _hash_field(hasher, path)
        for field in _path_fact(root=root, relative=path):
            _hash_field(hasher, field)

    return DirtyTreeDigest(
        algorithm=DIRTY_TREE_ALGORITHM,
        digest="sha256:" + hasher.hexdigest(),
        dirty=bool(entries),
        status_entry_count=len(entries),
        changed_path_count=len(relevant_paths),
    )


def _index_records(root: Path) -> tuple[bytes, ...]:
    records: list[bytes] = []
    output = _git(root, "ls-files", "--stage", "-z").stdout
    for raw in output.split(b"\x00"):
        if not raw:
            continue
        try:
            metadata, raw_path = raw.split(b"\t", 1)
        except ValueError as exc:
            raise GitEvidenceError("Git returned a malformed index entry") from exc
        fields = metadata.split(b" ")
        if len(fields) != 3:
            raise GitEvidenceError("Git returned malformed index metadata")
        mode, object_id, stage = fields
        path = _normalize_git_path(raw_path)
        if (
            not mode.isdigit()
            or len(object_id) not in {40, 64}
            or any(byte not in b"0123456789abcdef" for byte in object_id.lower())
            or stage not in {b"0", b"1", b"2", b"3"}
        ):
            raise GitEvidenceError("Git returned invalid index metadata")
        if _is_engine_path(path):
            continue
        records.append(metadata + b"\t" + path)
    return tuple(sorted(records))


def _path_fact(*, root: Path, relative: bytes) -> tuple[bytes, ...]:
    root_bytes = os.fsencode(root)
    _validate_git_path(relative)
    _reject_symlink_parents(root=root_bytes, relative=relative)
    path = os.path.join(root_bytes, relative)
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return (b"tombstone",)
    except OSError as exc:
        raise GitEvidenceError(
            f"cannot inspect changed path {os.fsdecode(relative)!r}: {exc}"
        ) from exc

    mode = metadata.st_mode
    common = (f"mode:{mode:o}".encode("ascii"),)
    if stat.S_ISREG(mode):
        return (
            b"type:regular",
            *common,
            b"sha256:" + _regular_file_sha256(path=path, display=relative),
        )
    if stat.S_ISLNK(mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise GitEvidenceError(
                f"cannot read changed symlink {os.fsdecode(relative)!r}: {exc}"
            ) from exc
        return (b"type:symlink", *common, _sha256(target).encode("ascii"))
    if stat.S_ISDIR(mode):
        return (
            b"type:directory",
            *common,
            b"tree-" + _directory_digest(path=path, display=relative),
        )
    if stat.S_ISFIFO(mode):
        kind = b"fifo"
    elif stat.S_ISSOCK(mode):
        kind = b"socket"
    elif stat.S_ISCHR(mode):
        kind = b"character-device"
    elif stat.S_ISBLK(mode):
        kind = b"block-device"
    else:
        kind = b"unknown"
    return (b"type:" + kind, *common)


def _directory_digest(*, path: bytes, display: bytes) -> bytes:
    """Content-bind a changed directory without following symlinks.

    Git reports an embedded repository as one directory status entry (and a
    submodule as one gitlink entry).  Hashing only that directory's mode would
    therefore make later edits beneath an already-dirty boundary invisible.
    This Merkle-style digest walks the working-tree content while deliberately
    omitting Git metadata and loopy-loop's runtime state.  Symlinks contribute
    only their link text, so a link cannot make evidence escape the repository.
    """

    hasher = hashlib.sha256()
    _hash_field(hasher, b"loopy-directory-tree-v1")
    try:
        with os.scandir(path) as iterator:
            entries = sorted(iterator, key=lambda item: os.fsencode(item.name))
    except OSError as exc:
        raise GitEvidenceError(
            f"cannot inspect changed directory {os.fsdecode(display)!r}: {exc}"
        ) from exc
    for entry in entries:
        name = os.fsencode(entry.name)
        child_display = display + b"/" + name
        if name in {b".git", ENGINE_RUNTIME_DIR} or _is_engine_path(child_display):
            continue
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise GitEvidenceError(
                f"cannot inspect changed path {os.fsdecode(child_display)!r}: {exc}"
            ) from exc
        _hash_field(hasher, b"entry")
        _hash_field(hasher, name)
        for field in _directory_entry_fact(
            path=os.fsencode(entry.path), display=child_display, metadata=metadata
        ):
            _hash_field(hasher, field)
    return b"sha256:" + hasher.hexdigest().encode("ascii")


def _directory_entry_fact(
    *, path: bytes, display: bytes, metadata: os.stat_result
) -> tuple[bytes, ...]:
    mode = metadata.st_mode
    common = (f"mode:{mode:o}".encode("ascii"),)
    if stat.S_ISREG(mode):
        return (
            b"type:regular",
            *common,
            b"sha256:" + _regular_file_sha256(path=path, display=display),
        )
    if stat.S_ISLNK(mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise GitEvidenceError(
                f"cannot read changed symlink {os.fsdecode(display)!r}: {exc}"
            ) from exc
        return (b"type:symlink", *common, _sha256(target).encode("ascii"))
    if stat.S_ISDIR(mode):
        return (
            b"type:directory",
            *common,
            b"tree-" + _directory_digest(path=path, display=display),
        )
    if stat.S_ISFIFO(mode):
        kind = b"fifo"
    elif stat.S_ISSOCK(mode):
        kind = b"socket"
    elif stat.S_ISCHR(mode):
        kind = b"character-device"
    elif stat.S_ISBLK(mode):
        kind = b"block-device"
    else:
        kind = b"unknown"
    return (b"type:" + kind, *common)


def _regular_file_sha256(*, path: bytes, display: bytes) -> bytes:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise GitEvidenceError(
            f"cannot hash changed path {os.fsdecode(display)!r}: {exc}"
        ) from exc
    return digest.hexdigest().encode("ascii")


def _reject_symlink_parents(*, root: bytes, relative: bytes) -> None:
    """Reject a Git path whose intermediate component became a symlink."""

    cursor = root
    for component in relative.split(b"/")[:-1]:
        cursor = os.path.join(cursor, component)
        try:
            metadata = os.lstat(cursor)
        except OSError as exc:
            raise GitEvidenceError(
                f"cannot inspect changed path {os.fsdecode(relative)!r}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise GitEvidenceError(
                f"changed path {os.fsdecode(relative)!r} has an unsafe parent"
            )


def _sanitize_remote(raw_url: bytes) -> tuple[str, str | None, bytes]:
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
        transport = "file" if scheme == "file" else scheme
        return transport, hostname, canonical

    # Git's scp-like syntax is [user@]host:path. A slash before the first
    # colon identifies a local filesystem path instead.
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
    normalized_path = path.replace("\\", "/").strip("/")
    if normalized_path.endswith(".git"):
        normalized_path = normalized_path[:-4]
    return json.dumps(
        {"host": hostname or "", "port": port, "repository_path": normalized_path},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8", "surrogatepass")


def _verbose_diff(root: Path) -> bytes:
    common = (
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "--",
        ".",
        ":(exclude).loopy_loop",
        ":(exclude).loopy_loop/**",
    )
    unstaged = _git(root, "diff", *common).stdout
    staged = _git(root, "diff", "--cached", *common).stdout
    return b"# unstaged\n" + unstaged + b"\n# staged\n" + staged


def _render_status(entries: tuple[_StatusEntry, ...]) -> bytes:
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


def _write_bytes_atomic(path: Path, content: bytes) -> None:
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
    root: Path, *arguments: str, allowed: tuple[int, ...] = (0,)
) -> subprocess.CompletedProcess[bytes]:
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


def _validate_git_path(path: bytes) -> None:
    if (
        not path
        or path.startswith(b"/")
        or b"\x00" in path
        or any(part in {b"", b".", b".."} for part in path.split(b"/"))
    ):
        raise GitEvidenceError("Git returned an unsafe working-tree path")


def _normalize_git_path(path: bytes) -> bytes:
    """Normalize Git's directory presentation without relaxing path safety."""

    normalized = path[:-1] if path.endswith(b"/") else path
    _validate_git_path(normalized)
    return normalized


def _is_engine_path(path: bytes) -> bool:
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


def _hash_field(hasher: object, value: bytes) -> None:
    assert isinstance(hasher, type(hashlib.sha256()))
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()
