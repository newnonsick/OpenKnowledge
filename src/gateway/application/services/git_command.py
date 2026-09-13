from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class GitCommandError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class GitChange:
    change_type: str
    path: str


def normalize_repo_root(repo: str) -> str:
    candidate = repo.strip()
    if candidate.startswith("file://"):
        parsed = urlparse(candidate)
        if parsed.netloc not in ("", "localhost"):
            raise GitCommandError("invalid_repo", "Only local file URLs are supported for git connectors.")
        candidate = parsed.path
    elif "://" in candidate:
        scheme = candidate.split("://", 1)[0]
        if not (len(scheme) == 1 or scheme.lower() in ("file",)):
            raise GitCommandError("invalid_repo", "Only local repository paths are supported for git connectors.")
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        raise GitCommandError("invalid_repo", "The repository path must be absolute.")
    return str(path)


async def _run_git(root: str, *args: str, timeout_seconds: float) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            root,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise GitCommandError("git_unavailable", "The git executable is not available on this host.") from exc
    except OSError as exc:
        raise GitCommandError("repo_unavailable", "The repository could not be accessed.") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError as exc:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        raise GitCommandError("repo_unavailable", "The git command timed out.") from exc
    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise GitCommandError("repo_unavailable", detail[0][:300] if detail else "The git command failed.")
    return stdout or b""


def git_is_repository(root: str) -> bool:
    try:
        path = Path(root)
    except (ValueError, OSError):
        return False
    if not path.is_dir():
        return False
    return (path / ".git").exists() or (path / "HEAD").exists()


async def _resolve_ref(root: str, ref: str, timeout_seconds: float) -> str:
    output = await _run_git(root, "rev-parse", "--verify", f"refs/heads/{ref}^{{commit}}", timeout_seconds=timeout_seconds)
    return output.decode("ascii", "replace").strip()


def validate_branch_name(branch: str) -> str:
    name = branch.strip()
    if not name or len(name) > 255 or name != branch.strip():
        raise GitCommandError("invalid_branch", "A valid branch name is required.")
    if (
        name.startswith("-")
        or name.startswith("/")
        or name.endswith("/")
        or name.endswith(".lock")
        or "//" in name
        or ".." in name
        or any(character in name for character in ("~", "^", ":", "?", "*", "[", "\\", " ", "\t", "\n"))
        or "@{" in name
    ):
        raise GitCommandError("invalid_branch", "A valid branch name is required.")
    return name


async def git_branch_commit(root: str, branch: str, *, timeout_seconds: float) -> str:
    name = validate_branch_name(branch)
    try:
        return await _resolve_ref(root, name, timeout_seconds)
    except GitCommandError as exc:
        raise GitCommandError("invalid_branch", "The branch does not exist in the repository.") from exc


async def git_head_commit(root: str, branch: str, *, timeout_seconds: float) -> str:
    return await git_branch_commit(root, branch, timeout_seconds=timeout_seconds)


async def git_changed_paths(
    root: str, base_commit: str | None, head_commit: str, *, timeout_seconds: float
) -> list[GitChange]:
    if base_commit is None:
        output = await _run_git(root, "ls-tree", "-r", "--name-only", "-z", head_commit, timeout_seconds=timeout_seconds)
        paths = [entry for entry in output.decode("utf-8", "replace").split("\x00") if entry]
        return [GitChange(change_type="added", path=path) for path in sorted(paths)]
    output = await _run_git(
        root,
        "diff-tree",
        "-r",
        "--no-commit-id",
        "--name-status",
        "-z",
        "--no-renames",
        base_commit,
        head_commit,
        timeout_seconds=timeout_seconds,
    )
    text = output.decode("utf-8", "replace")
    tokens = [token for token in text.split("\x00") if token]
    changes: list[GitChange] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if index >= len(tokens):
            break
        path = tokens[index]
        index += 1
        if not status or not path or "\n" in path or path.startswith("-"):
            continue
        code = status[0]
        if code == "A":
            changes.append(GitChange(change_type="added", path=path))
        elif code == "D":
            changes.append(GitChange(change_type="deleted", path=path))
        elif code == "M" or code == "T":
            changes.append(GitChange(change_type="modified", path=path))
    return changes


async def git_file_bytes(
    root: str, commit: str, path: str, *, max_bytes: int, timeout_seconds: float
) -> bytes:
    if not path or "\n" in path or path.startswith("-") or path.startswith("/"):
        raise GitCommandError("file_missing", "The repository path is not syncable.")
    try:
        size_output = await _run_git(root, "cat-file", "-s", f"{commit}:{path}", timeout_seconds=timeout_seconds)
    except GitCommandError as exc:
        raise GitCommandError("file_missing", "The file is not present at the synced commit.") from exc
    try:
        size = int(size_output.decode("ascii", "replace").strip())
    except ValueError as exc:
        raise GitCommandError("file_missing", "The file is not present at the synced commit.") from exc
    if size > max_bytes:
        raise GitCommandError("oversize", "The file exceeds the upload size limit.")
    content = await _run_git(root, "cat-file", "blob", f"{commit}:{path}", timeout_seconds=timeout_seconds)
    return content[: max_bytes + 1]
