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

_ABS_PATH = re.compile(r"(?<!\{REPO\})/[\w\-.~]+(?:/[\w\-.~]+)+")
_STAMP = re.compile(r"\d{8}T\d{6}")
STAMP = "{STAMP}"
_NULL = "\0devnull"


def _normalize(text: str, repo: Path, *, for_match: bool) -> str:
    """Replace run-specific paths so cassettes match on any machine.

    Stored output gets only the repository swap: mangling anything else
    would corrupt text that is rejoined into paths and normalized again.
    Match keys additionally collapse every other absolute path (temporary
    directories git is handed), which differs on every run by design.
    """
    out = text
    for base in {str(repo), str(Path(repo).resolve())}:
        out = out.replace(base, REPO)
    if not for_match:
        return out
    out = _STAMP.sub(STAMP, out)
    out = out.replace("/dev/null", _NULL)
    out = _ABS_PATH.sub(ABS, out)
    return out.replace(_NULL, "/dev/null")


def _key(args: tuple[str, ...], stdin: str | None, repo: Path) -> dict[str, Any]:
    return {
        "args": [_normalize(arg, repo, for_match=True) for arg in args],
        "stdin": _normalize(stdin, repo, for_match=True) if stdin is not None else None,
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

    def consume_git(self, key: dict[str, Any]) -> dict[str, Any]:
        self._ensure_loaded()
        if self.index >= len(self.frames):
            raise AssertionError(
                f"cassette {self.path} exhausted at call {self.index + 1}: "
                f"unexpected git {key['args']}"
            )
        frame = self.frames[self.index]
        if (
            frame.get("type") != "git"
            or frame["args"] != key["args"]
            or frame["stdin"] != key["stdin"]
        ):
            raise AssertionError(
                f"cassette {self.path} diverged at call {self.index + 1}:\n"
                f"  recorded: {frame.get('type')} "
                f"{frame.get('args', frame.get('op'))}\n"
                f"  received: git {key['args']}"
            )
        self.index += 1
        return frame

    def consume_fs(self, op: str, path: str) -> dict[str, Any]:
        self._ensure_loaded()
        if self.index >= len(self.frames):
            raise AssertionError(
                f"cassette {self.path} exhausted at call {self.index + 1}: "
                f"unexpected fs {op} {path}"
            )
        frame = self.frames[self.index]
        if (
            frame.get("type") != "fs"
            or frame["op"] != op
            or frame["path"] != path
        ):
            raise AssertionError(
                f"cassette {self.path} diverged at call {self.index + 1}:\n"
                f"  recorded: {frame.get('type')} "
                f"{frame.get('args', frame.get('op'))}\n"
                f"  received: fs {op} {path}"
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


class HarnessGit(Git):
    """A Git whose gitdir paths log their probes to the cassette.

    Git state is read two ways -- subprocess output and direct filesystem
    probes (`rebase-merge/` existing, `msgnum` contents) -- and replaying
    only the first diverges as soon as real git hasn't created the second.
    `git_path` therefore returns a `LoggedPath`, so both go through the
    one ordered frame stream. Working-tree files stay real: only paths
    under the git dir are proxied.
    """

    def __init__(self, repo: Path, store: CassetteStore) -> None:
        super().__init__(repo)
        self._store = store

    def git_path(self, name: str) -> Any:
        raw = self.out("rev-parse", "--git-path", name)
        path = Path(raw)
        real = path if path.is_absolute() else self.repo / path
        return LoggedPath(real, self._store, self.repo)


class LoggedPath:
    """A path inside the git dir whose reads the cassette answers."""

    def __init__(self, real: Path, store: CassetteStore, repo: Path) -> None:
        self._real = real
        self._store = store
        self._repo = repo

    def _key(self) -> str:
        return _normalize(str(self._real), self._repo, for_match=True)

    def __truediv__(self, other: str | Path) -> LoggedPath:
        return LoggedPath(self._real / other, self._store, self._repo)

    def __str__(self) -> str:
        return str(self._real)

    def __fspath__(self) -> str:
        return str(self._real)

    def is_dir(self) -> bool:
        return self._probe("is_dir", lambda: self._real.is_dir())

    def exists(self) -> bool:
        return self._probe("exists", lambda: self._real.exists())

    def is_file(self) -> bool:
        return self._probe("is_file", lambda: self._real.is_file())

    def read_text(self, *args: Any, **kwargs: Any) -> str:
        return self._probe("read_text", lambda: self._real.read_text(*args, **kwargs))

    def write_text(self, *args: Any, **kwargs: Any) -> int:
        return self._probe("write_text", lambda: self._real.write_text(*args, **kwargs))

    def unlink(self, *args: Any, **kwargs: Any) -> None:
        self._probe("unlink", lambda: self._real.unlink(*args, **kwargs))
        return None

    def _probe(self, op: str, call: Any) -> Any:
        if self._store.record:
            try:
                result = call()
            except OSError as exc:
                self._store.append(
                    {"type": "fs", "op": op, "path": self._key(), "ok": False,
                     "error": type(exc).__name__}
                )
                raise
            self._store.append(
                {"type": "fs", "op": op, "path": self._key(), "ok": True,
                 "result": result}
            )
            return result
        frame = self._store.consume_fs(op, self._key())
        if not frame["ok"]:
            raise OSError(f"replayed fs {op} {self._key()} failed")
        return frame["result"]


class RecordingGit(HarnessGit):
    """Real git that writes every answer to the cassette."""

    def run(self, *args: str, check: bool = True, stdin: str | None = None) -> GitResult:
        result = super().run(*args, check=False, stdin=stdin)
        self._store.append(
            {
                "type": "git",
                **_key(args, stdin, self.repo),
                "returncode": result.returncode,
                "stdout": _normalize(result.stdout, self.repo, for_match=False),
                "stderr": _normalize(result.stderr, self.repo, for_match=False),
            }
        )
        if check and not result.ok:
            raise GitError(result)
        return result


class ReplayGit(HarnessGit):
    """No subprocess: answers come from the cassette in order."""

    def run(self, *args: str, check: bool = True, stdin: str | None = None) -> GitResult:
        frame = self._store.consume_git(_key(args, stdin, self.repo))
        result = GitResult(
            args,
            frame["returncode"],
            frame["stdout"].replace(REPO, str(self.repo)),
            frame["stderr"].replace(REPO, str(self.repo)),
        )
        if check and not result.ok:
            raise GitError(result)
        return result
