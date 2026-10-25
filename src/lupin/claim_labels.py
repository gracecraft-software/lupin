"""Keep the GitHub `claimed` label aligned with Redis issue claims."""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from urllib.parse import urlencode

from . import claims

LABEL = "claimed"
_LABEL_COLOR = "d73a4a"
_LABEL_DESCRIPTION = "A Lupin worker has claimed this issue."
_API_TIMEOUT = 20.0
_STATUS_RE = re.compile(r"(?m)^HTTP/\S+\s+(\d{3})(?:\s+([^\r\n]*))?")


class GitHubAPIError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        self.status = status
        super().__init__(message)


class ClaimLabelSyncError(RuntimeError):
    """A Redis claim changed, but its GitHub label did not sync."""


def _api(
    method: str,
    endpoint: str,
    *,
    fields: tuple[str, ...] = (),
    paginate: bool = False,
    include: bool = False,
):
    command = ["gh", "api"]
    if include:
        command.append("--include")
    if paginate:
        command.extend(["--paginate", "--slurp"])
    command.extend(["--method", method, endpoint])
    for field in fields:
        command.extend(["--field", field])
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_API_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitHubAPIError(None, f"could not run `gh api`: {exc}") from exc

    status = None
    body = result.stdout
    if include:
        matches = list(_STATUS_RE.finditer(result.stdout))
        if matches:
            response = matches[-1]
            status = int(response.group(1))
            body_start = re.search(r"\r?\n\r?\n", result.stdout[response.end() :])
            if body_start:
                body = result.stdout[response.end() + body_start.end() :]
            else:
                body = ""
    if result.returncode != 0 or (status is not None and not 200 <= status < 300):
        message = result.stderr.strip()
        if body:
            try:
                detail = json.loads(body).get("message")
            except (json.JSONDecodeError, AttributeError):
                detail = None
            if detail:
                message = str(detail)
        if not message:
            message = f"request failed with exit code {result.returncode}"
        raise GitHubAPIError(status, f"HTTP {status}: {message}" if status else message)
    if not body.strip():
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise GitHubAPIError(status, "GitHub returned invalid JSON") from exc


def _issue_parts(target: str) -> tuple[str, str, str]:
    owner_repo, number = claims.parse_target(target).rsplit("#", 1)
    owner, repo = owner_repo.split("/", 1)
    return owner, repo, number


def _ensure_repository_label(owner: str, repo: str) -> None:
    endpoint = f"repos/{owner}/{repo}/labels/{LABEL}"
    try:
        _api("GET", endpoint, include=True)
        return
    except GitHubAPIError as exc:
        if exc.status != 404:
            raise

        try:
            _api(
                "POST",
                f"repos/{owner}/{repo}/labels",
                fields=(
                    f"name={LABEL}",
                    f"color={_LABEL_COLOR}",
                    f"description={_LABEL_DESCRIPTION}",
                ),
                include=True,
            )
        except GitHubAPIError as create_error:
            if create_error.status != 422:
                raise
            # Another worker may have created the label after the first GET.
            _api("GET", endpoint, include=True)


def add_claimed_label(target: str) -> None:
    owner, repo, number = _issue_parts(target)
    issue = _api("GET", f"repos/{owner}/{repo}/issues/{number}")
    if not isinstance(issue, dict):
        raise GitHubAPIError(None, "GitHub returned invalid issue data")
    if "pull_request" in issue:
        remove_claimed_label(target)
        return
    _ensure_repository_label(owner, repo)
    _api(
        "POST",
        f"repos/{owner}/{repo}/issues/{number}/labels",
        fields=(f"labels[]={LABEL}",),
    )


def remove_claimed_label(target: str) -> None:
    owner, repo, number = _issue_parts(target)
    endpoint = f"repos/{owner}/{repo}/issues/{number}/labels?per_page=100"
    pages = _api("GET", endpoint, paginate=True)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise GitHubAPIError(None, "GitHub returned invalid issue labels")
    if not any(
        isinstance(label, dict) and label.get("name") == LABEL
        for page in pages
        for label in page
    ):
        return
    try:
        _api("DELETE", f"repos/{owner}/{repo}/issues/{number}/labels/{LABEL}", include=True)
    except GitHubAPIError as exc:
        if exc.status != 404:
            raise



def _has_casefold_claim(target: str, **connection) -> bool:
    owner_repo, number = claims.parse_target(target).rsplit("#", 1)
    identity = (owner_repo.casefold(), int(number))
    for other in claims._scan_claim_values(**connection):
        try:
            other_owner_repo, other_number = claims.parse_target(other).rsplit("#", 1)
        except ValueError:
            continue
        if (other_owner_repo.casefold(), int(other_number)) == identity:
            return True
    return False


def sync_claimed_label(
    target: str, *, github_target: str | None = None, **connection
) -> None:
    """Make the GitHub label match the current Redis claim."""
    label_target = target if github_target is None else github_target
    while True:
        active = claims._mark_label_index_if_active(target, **connection)
        if not active and _has_casefold_claim(target, **connection):
            claims._mark_label_index(target, **connection)
            active = True

        if active:
            try:
                add_claimed_label(label_target)
            except GitHubAPIError:
                claims._mark_label_index(target, **connection)
                raise
            claims._mark_label_index(target, **connection)
            if claims._mark_label_index_if_active(target, **connection):
                return
            if _has_casefold_claim(target, **connection):
                if claims._clear_label_index_if_unclaimed(target, **connection):
                    if _has_casefold_claim(target, **connection):
                        return
                continue

        remove_claimed_label(label_target)
        if claims._clear_label_index_if_unclaimed(target, **connection):
            if not _has_casefold_claim(target, **connection):
                return


def list_claimed_issues(owner: str, repo: str) -> list[int]:
    query = urlencode({"state": "all", "labels": LABEL, "per_page": 100})
    pages = _api(
        "GET",
        f"repos/{owner}/{repo}/issues?{query}",
        paginate=True,
    )
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise GitHubAPIError(None, "GitHub returned invalid claimed issue data")
    numbers = set()
    for page in pages:
        for item in page:
            if isinstance(item, dict) and isinstance(item.get("number"), int):
                numbers.add(item["number"])
    return sorted(numbers)


def claim(
    target: str, holder: str, *, ttl: float = claims.DEFAULT_TTL, **connection
) -> bool:
    """Take a Redis claim, then apply its GitHub label."""
    generation = uuid.uuid4().hex
    created = claims.claim(
        target, holder, ttl=ttl, generation=generation, **connection
    )
    try:
        sync_claimed_label(target, **connection)
    except GitHubAPIError as exc:
        if not created:
            raise ClaimLabelSyncError(
                f"could not sync the {LABEL!r} label for {target}: {exc}; "
                "the existing Redis claim remains active"
            ) from exc
        try:
            rolled_back = claims.release_claim(
                target, holder, expected_generation=generation, **connection
            )
        except Exception as rollback_error:
            raise ClaimLabelSyncError(
                f"could not sync the {LABEL!r} label for {target}: {exc}; "
                f"could not confirm Redis rollback: {rollback_error}"
            ) from exc
        if not rolled_back:
            raise ClaimLabelSyncError(
                f"could not add the {LABEL!r} label to {target}: {exc}; "
                "could not confirm that the Redis claim was released"
            ) from exc
        raise ClaimLabelSyncError(
            f"could not sync the {LABEL!r} label for {target}: {exc}; "
            "the Redis claim was released"
        ) from exc
    return created


def renew_claim(
    target: str,
    holder: str,
    *,
    ttl: float = claims.DEFAULT_TTL,
    **connection,
) -> bool:
    renewed = claims.renew_claim(target, holder, ttl=ttl, **connection)
    if not renewed:
        return False
    try:
        sync_claimed_label(target, **connection)
    except GitHubAPIError as exc:
        raise ClaimLabelSyncError(
            f"renewed the Redis claim for {target}, but could not ensure the "
            f"{LABEL!r} label: {exc}; the claim remains active"
        ) from exc
    return True


def renew_holder_claims(
    holder: str, *, ttl: float = claims.DEFAULT_TTL, **connection
) -> list[str]:
    renewed = claims.renew_holder_claims(holder, ttl=ttl, **connection)
    errors = []
    for target in renewed:
        try:
            sync_claimed_label(target, **connection)
        except GitHubAPIError as exc:
            errors.append(f"{target}: {exc}")
    if errors:
        raise ClaimLabelSyncError(
            f"renewed claims remain active, but could not ensure the {LABEL!r} label: "
            + "; ".join(errors)
        )
    return renewed


def release_claim(target: str, holder: str, **connection) -> bool:
    released = claims.release_claim(target, holder, **connection)
    if not released:
        return False

    try:
        sync_claimed_label(target, **connection)
    except GitHubAPIError as exc:
        raise ClaimLabelSyncError(
            f"released the Redis claim for {target}, but could not sync the "
            f"{LABEL!r} label: {exc}"
        ) from exc
    return True
