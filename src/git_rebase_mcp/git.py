"""A thin, typed wrapper around the git command line.

Shelling out rather than binding to libgit2 is deliberate. This server's value
is that its behaviour matches what you would get by running the same commands by
hand; a library that disagreed with git in some corner would undermine the whole
point of it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class GitError(Exception):
    """A git command that was expected to succeed did not."""

    def __init__(self, result: GitResult) -> None:
        self.result = result
        command = " ".join(("git", *result.args))
        detail = (result.stderr or result.stdout).strip()
        super().__init__(f"{command} exited {result.returncode}: {detail}")


class Git:
    """Runs git in one repository.

    The environment is pinned so that output is parseable and nothing can block:
    no pager waiting on a terminal, no credential prompt, and messages in a
    known language rather than the user's locale.
    """

    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo)

    def run(self, *args: str, check: bool = True, stdin: str | None = None) -> GitResult:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            input=stdin,
            env={
                "PATH": _path(),
                "HOME": str(Path.home()),
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "LC_ALL": "C",
                **_extra_env(),
            },
        )
        result = GitResult(args, completed.returncode, completed.stdout, completed.stderr)
        if check and not result.ok:
            raise GitError(result)
        return result

    def out(self, *args: str) -> str:
        """Standard output, trailing newline removed."""
        return self.run(*args).stdout.rstrip("\n")

    def lines(self, *args: str) -> list[str]:
        """Standard output split into lines, with no empty trailing entry."""
        text = self.out(*args)
        return text.split("\n") if text else []

    def git_path(self, name: str) -> Path:
        """Resolve a path inside the git directory.

        Asking git rather than assuming `.git/` keeps this working in worktrees
        and submodules, where the git directory is somewhere else entirely.
        """
        path = Path(self.out("rev-parse", "--git-path", name))
        return path if path.is_absolute() else self.repo / path

    def succeeds(self, *args: str) -> bool:
        """Whether the command exits zero. For questions, not for actions."""
        return self.run(*args, check=False).ok

    def where(self) -> tuple[str, str]:
        """The working tree this instance drives, and the branch checked out.

        Reported back to the caller because a `repo` argument is easy to get
        right and a shell is not: a repository with worktrees has several
        checkouts of the same history, each on its own branch and at its own
        commit, and a plain `git show HEAD:file` answers about whichever
        directory the shell happens to be in.

        Asking rather than echoing the argument, so a relative path, a
        subdirectory of the repository and a symlink all come back as the one
        place the answer is about. Empty strings when git cannot say -- a
        report is worth returning without them.

        A rebase detaches HEAD, so `--abbrev-ref` answers "HEAD" for the whole
        of the operation these reports are read during -- which is to say, the
        field would be blank exactly where it was added to be useful. Git
        records what it is rewriting in `rebase-merge/head-name`, and that is
        the branch a caller means.
        """
        top = self.run("rev-parse", "--show-toplevel", check=False)
        return (top.stdout.strip() if top.ok else "", self._branch())

    def _branch(self) -> str:
        name = self.run("rev-parse", "--abbrev-ref", "HEAD", check=False)
        current = name.stdout.strip() if name.ok else ""
        if current != "HEAD":
            return current
        try:
            rewriting = self.git_path("rebase-merge/head-name").read_text().strip()
        except OSError:
            # Detached for some other reason, which is the honest answer: there
            # is no branch, and naming one would be worse than naming none.
            return ""
        return rewriting.removeprefix("refs/heads/")


def _path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


# Env vars passed through to git when set. Absent means git falls back to
# config, so production is unchanged; the test session sets these once
# instead of running `git config` in every scratch repository.
_PASSTHROUGH_PREFIXES = ("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_PASSTHROUGH_EXACT = frozenset({"GIT_CONFIG_COUNT"})


def _extra_env() -> dict[str, str]:
    import os

    extra: dict[str, str] = {}
    for key, value in os.environ.items():
        if (key in _PASSTHROUGH_EXACT or key.startswith(_PASSTHROUGH_PREFIXES)) and value.strip():
            extra[key] = value
    return extra
