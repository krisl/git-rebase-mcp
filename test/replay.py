"""Record and replay the git subprocess calls one test makes.

`--record` shells out to real git and stores every answer under
`test/cassettes/`; plain `pytest` replays those answers with no subprocess;
`--real-git` always runs the thorough suite against real git.

Matching is on normalized args: the test's own repository path becomes
`{REPO}` and any other absolute path (temporary directories git is handed)
becomes `{ABS}`. Dynamic values that flow through recorded outputs -- shas,
timestamps -- stay consistent on their own, because the test feeds the
recorded answer back into the next call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from git_rebase_mcp.git import Git, GitError, GitResult

CASSETTE_DIR = Path(__file__).parent / "cassettes"

REPO = "{REPO}"
ABS = "{ABS}"

_ABS_PATH = re.compile(r"/[\w\-.~]+(?:/[\w\-.~]+)+")
_NULL = "\0devnull"


def _normalize(text: str, repo: Path) -> str:
    """Replace run-specific paths so cassettes match on any machine."""
    out = text
    for base in {str(repo), str(Path(repo).resolve())}:
        out = out.replace(base, REPO)
    out = out.replace("/dev/null", _NULL)
    out = _ABS_PATH.sub(ABS, out)
    return out.replace(_NULL, "/dev/null")


def _key(args: tuple[str, ...], stdin: str | None, repo: Path) -> dict[str, Any]:
    return {
        "args": [_normalize(arg, repo) for arg in args],
        "stdin": _normalize(stdin, repo) if stdin is not None else None,
    }


class CassetteStore:
    """The frames of one test, shared by every Git instance it creates."""

    def __init__(self, path: Path, *, record: bool) -> None:
        self.path = path
        self.record = record
        self.frames: list[dict[str, Any]] = []
        self.index = 0
        self._loaded = record

    def exists(self) -> bool:
        return self.path.is_file()

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.frames = json.loads(self.path.read_text())["frames"]
            self._loaded = True

    @classmethod
    def for_node(cls, node_id: str, *, record: bool) -> CassetteStore:
        module, _, name = node_id.partition("::")
        stem = Path(module).stem
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return cls(CASSETTE_DIR / stem / f"{safe}.json", record=record)

    def factory(self, repo: Path) -> Git:
        if self.record:
            return RecordingGit(repo, self)
        return ReplayGit(repo, self)

    def append(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    def consume(self, key: dict[str, Any], node_id: str) -> dict[str, Any]:
        self._ensure_loaded()
        if self.index >= len(self.frames):
            raise AssertionError(
                f"cassette {self.path} exhausted at call {self.index + 1} "
                f"in {node_id}: unexpected git {key['args']}"
            )
        frame = self.frames[self.index]
        if frame["args"] != key["args"] or frame["stdin"] != key["stdin"]:
            raise AssertionError(
                f"cassette {self.path} diverged at call {self.index + 1} in {node_id}:\n"
                f"  recorded: git {frame['args']}\n"
                f"  received: git {key['args']}"
            )
        self.index += 1
        return frame

    def finalize(self) -> None:
        if self.record:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"frames": self.frames}, indent=1) + "\n")
            return
        self._ensure_loaded()
        if self.index != len(self.frames):
            raise AssertionError(
                f"cassette {self.path} has {len(self.frames) - self.index} "
                "unused frames: the test stopped calling git early"
            )


class RecordingGit(Git):
    """Real git that writes every answer to the cassette."""

    def __init__(self, repo: Path, store: CassetteStore) -> None:
        super().__init__(repo)
        self._store = store

    def run(self, *args: str, check: bool = True, stdin: str | None = None) -> GitResult:
        result = super().run(*args, check=False, stdin=stdin)
        self._store.append(
            {
                **_key(args, stdin, self.repo),
                "returncode": result.returncode,
                "stdout": _normalize(result.stdout, self.repo),
                "stderr": _normalize(result.stderr, self.repo),
            }
        )
        if check and not result.ok:
            raise GitError(result)
        return result


class ReplayGit(Git):
    """No subprocess: answers come from the cassette in order."""

    def __init__(self, repo: Path, store: CassetteStore) -> None:
        super().__init__(repo)
        self._store = store

    def run(self, *args: str, check: bool = True, stdin: str | None = None) -> GitResult:
        frame = self._store.consume(_key(args, stdin, self.repo), str(self.repo))
        result = GitResult(
            args,
            frame["returncode"],
            frame["stdout"].replace(REPO, str(self.repo)),
            frame["stderr"].replace(REPO, str(self.repo)),
        )
        if check and not result.ok:
            raise GitError(result)
        return result
