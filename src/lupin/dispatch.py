"""`lupin dispatch`: claim one issue/PR, brief its worker, and start the loop.

Issue #181. The brief is markdown, built by `review_dispatch.brief_markdown`
from the same fetch that `review-route --prefetch` uses. It lives outside
the repository, under the loop state directory's `notes/`, so it is never
committed and a worker never has to decode JSON to read it.

The order matters: claim first, then fetch, then write the brief, then start
the loop. A failed fetch or a failed brief write releases the claim instead
of leaving a worker to run on a partial brief. A failed start keeps the
claim: the work is still this holder's, and the caller can retry. A brief
longer than `loop_runtime.MAX_NOTE_CHARS` is not passed as the start note;
the note names the brief path instead, which the loop user can read.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import claims
from . import claim_labels
from . import loop_runtime
from . import review_dispatch


class DispatchError(Exception):
    """A dispatch step failed. `code` is the process exit code to report."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def parse_item(item: str) -> tuple[str, str, int]:
    """Split a strictly valid `OWNER/REPO#N` into its parts.

    Reuses `claims.parse_target` so the CLI accepts exactly the same target
    shape as `claim`/`release-claim`.
    """
    target = claims.parse_target(item)
    owner_repo, number = target.rsplit("#", 1)
    owner, repo = owner_repo.split("/", 1)
    return owner, repo, int(number)


def _state_dir(state_dir: str | Path | None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    return Path(os.environ.get("LUPIN_LOOP_STATE_DIR", str(loop_runtime.STATE_DIR)))


def write_brief(item: str, brief: str, *, state_dir: str | Path | None = None) -> Path:
    """Write the markdown brief outside the repo and return its path."""
    owner, repo, number = parse_item(item)
    notes = _state_dir(state_dir) / "notes"
    notes.mkdir(parents=True, exist_ok=True, mode=0o750)
    path = notes / f"dispatch-{owner}-{repo}-{number}-{time.time_ns()}.md"
    path.write_text(brief, encoding="utf-8")
    os.chmod(path, 0o640)
    return path


def dispatch(
    item: str,
    holder: str | None,
    *,
    no_start: bool = False,
    state_dir: str | Path | None = None,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> dict:
    """Claim `item`, write its brief, and (unless `no_start`) start its loop.

    Raises `DispatchError` for a missing holder, a failed fetch, a failed
    brief write, or a start that returned false. A failed fetch or write
    releases a new claim from this call; an existing same-holder claim stays
    active. `no_start` and a failed start keep the claim.
    """
    if not holder:
        raise DispatchError("dispatch needs --holder or LUPIN_CLAIM_HOLDER")
    owner, repo, number = parse_item(item)
    target = f"{owner}/{repo}#{number}"
    connection = {
        "redis_host": redis_host,
        "redis_port": redis_port,
        "redis_username": redis_username,
        "redis_password": redis_password,
    }

    created = claim_labels.claim(target, holder, **connection)

    fetched = review_dispatch.prefetch([str(number)], repo=f"{owner}/{repo}")
    data = fetched[str(number)]
    if "error" in data:
        if created:
            try:
                claim_labels.release_claim(target, holder, **connection)
            except Exception as cleanup_error:
                raise DispatchError(
                    f"{data['error']}; could not clean up the claim: {cleanup_error}"
                ) from cleanup_error
        raise DispatchError(data["error"])

    brief = review_dispatch.brief_markdown(target, data)
    try:
        path = write_brief(target, brief, state_dir=state_dir)
    except OSError as error:
        if created:
            try:
                claim_labels.release_claim(target, holder, **connection)
            except Exception as cleanup_error:
                raise DispatchError(
                    f"could not write the brief for {target}: {error}; "
                    f"could not clean up the claim: {cleanup_error}"
                ) from error
        raise DispatchError(f"could not write the brief for {target}: {error}") from error

    started = False
    message = ""
    if not no_start:
        note = brief
        if len(brief) > loop_runtime.MAX_NOTE_CHARS:
            note = f"Read the brief at {path}."
        try:
            started, message = loop_runtime.start_loop(repo, note=note)
        except loop_runtime.LoopError as exc:
            raise DispatchError(str(exc)) from exc
        if not started:
            raise DispatchError(message or f"could not start the loop for {repo}")

    return {
        "item": target,
        "kind": data.get("kind"),
        "brief_path": str(path),
        "claim": holder,
        "started": started,
        "message": message,
    }
