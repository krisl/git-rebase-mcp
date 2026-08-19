"""What each tool actually puts on the wire.

Every other test here calls the tools as functions, which is what everything
in-process does and which skips the registration wrapper entirely. So the result a
client receives -- the rendering, the trimmed fields, and above all whether the
structured half still satisfies the schema the tool published -- was covered by
nothing. Trimming empty fields broke five of the seven tools exercised here and no
test noticed.
"""

from __future__ import annotations

import pytest

from git_rebase_mcp.server import mcp

from scratch import Scratch


@pytest.fixture
def rebasing(scratch: Scratch) -> Scratch:
    """A rebase stopped on a conflict, so the interesting tools have something to say."""
    scratch.commit("base", f="one\nkeep\n")
    scratch.commit("second", f="two\nkeep\n")
    scratch.commit("third", f="three\nkeep\n")
    return scratch


async def call(name: str, **arguments: object):
    return await mcp.call_tool(name, arguments, None)


@pytest.mark.anyio
async def test_every_tool_of_a_whole_rebase_answers(rebasing: Scratch) -> None:
    """Each result validates against the schema its tool published.

    Driven as a sequence rather than one call each, because the states that carry
    empty required fields -- no steps left, nothing still conflicted, no commits
    with markers -- only exist part-way through a real one.
    """
    repo = str(rebasing.path)
    third = rebasing.git.out("rev-parse", "HEAD")

    await call("rebase_preflight", base="HEAD~1", repo=repo)
    await call("rebase_start", base="HEAD~1", repo=repo, todo=[f"pick {third}"], onto="HEAD~2")
    await call("status", repo=repo)
    await call("conflicts", repo=repo)
    await call("rebase_todo", repo=repo)
    await call("resolve", path="f", content="three\nkeep\n", repo=repo)
    await call("rebase_compare", repo=repo)
    await call("proceed", repo=repo)
    result = await call("rebase_finish", repo=repo, allow_change=True)

    assert result.structured_content is not None
    assert result.structured_content["ok"] is True


@pytest.mark.anyio
async def test_a_required_field_survives_being_empty(rebasing: Scratch) -> None:
    """The trim drops what a field defaults to, and a required field has no default.

    `dropped` on a preflight with nothing wrong is the case that broke: empty, and
    required, so leaving it out made the tool fail its own output schema.
    """
    result = await call("rebase_preflight", base="HEAD~1", repo=str(rebasing.path))

    assert result.structured_content is not None
    assert result.structured_content["dropped"] == []
    assert result.structured_content["safe_to_start"] is True


@pytest.mark.anyio
async def test_a_defaulted_field_at_its_default_is_left_out(rebasing: Scratch) -> None:
    """And the point of the trim: a quiet report says only what is true of it."""
    result = await call("status", repo=str(rebasing.path))

    assert result.structured_content is not None
    sent = result.structured_content
    # Said, because a rebase either is or is not in progress
    assert sent["state"] == "not_rebasing"
    assert "guidance" in sent
    # Not said: no step, no action, nothing conflicted, nothing auto-resolved
    for quiet in ("step", "action", "replaying", "conflicted_files", "auto_resolved",
                  "replayed_resolutions", "git_said", "finished", "check"):
        assert quiet not in sent, quiet


@pytest.mark.anyio
async def test_the_result_carries_a_reading_of_itself(rebasing: Scratch) -> None:
    """The other half of what a client is handed."""
    result = await call("status", repo=str(rebasing.path))

    assert result.content[0].type == "text"
    assert result.content[0].text.startswith("not_rebasing · ")
