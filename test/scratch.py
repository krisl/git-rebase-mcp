"""A small git repository under construction, for tests to build on."""

from __future__ import annotations

from pathlib import Path

from git_rebase_mcp.git import Git


class Scratch:
    """A small repository under construction."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.git = Git(path)

    def write(self, name: str, content: str) -> None:
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def read(self, name: str) -> str:
        return (self.path / name).read_text()

    def commit(self, message: str, **files: str) -> str:
        """Write files, commit them, and return the new sha."""
        for name, content in files.items():
            self.write(name.replace("__", "/"), content)
        self.git.run("add", "-A")
        self.git.run("commit", "-q", "-m", message)
        return self.git.out("rev-parse", "HEAD")

    def subjects(self, revision_range: str = "HEAD") -> list[str]:
        return self.git.lines("log", "--format=%s", revision_range)
