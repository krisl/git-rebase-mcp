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

    def succeeds(self, *args: str) -> bool:
        """Whether the command exits zero. For questions, not for actions."""
        return self.run(*args, check=False).ok


def _path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")
