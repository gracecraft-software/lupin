from __future__ import annotations

import subprocess
import time

import pytest

from lupin import claim_labels, claims, reconcile, slots


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def test_api_reports_http_status_from_gh_response(monkeypatch):
    response = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout=(
            "HTTP/2.0 404 Not Found\r\n"
            "Content-Type: application/json\r\n\r\n"
            '{"message":"Not Found"}'
        ),
        stderr="",
    )
    monkeypatch.setattr(claim_labels.subprocess, "run", lambda *args, **kwargs: response)

    with pytest.raises(claim_labels.GitHubAPIError, match="HTTP 404: Not Found") as exc:
        claim_labels._api("GET", "repos/acme/repo/labels/claimed", include=True)

    assert exc.value.status == 404


def test_add_creates_missing_repository_label_before_adding_issue_label(monkeypatch):
    calls = []

    def api(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        if endpoint == "repos/acme/repo/issues/17":
            return {"number": 17}
        if method == "GET":
            raise claim_labels.GitHubAPIError(404, "missing")

    monkeypatch.setattr(claim_labels, "_api", api)

    claim_labels.add_claimed_label("acme/repo#17")

    assert [call[:2] for call in calls] == [
        ("GET", "repos/acme/repo/issues/17"),
        ("GET", "repos/acme/repo/labels/claimed"),
        ("POST", "repos/acme/repo/labels"),
        ("POST", "repos/acme/repo/issues/17/labels"),
    ]
    assert "name=claimed" in calls[2][2]["fields"]
    assert calls[3][2]["fields"] == ("labels[]=claimed",)


def test_add_accepts_label_created_by_another_worker(monkeypatch):
    calls = []
    missing_label = True

    def api(method, endpoint, **kwargs):
        nonlocal missing_label
        calls.append((method, endpoint))
        if endpoint == "repos/acme/repo/issues/17":
            return {"number": 17}
        if method == "GET" and endpoint == "repos/acme/repo/labels/claimed":
            if missing_label:
                missing_label = False
                raise claim_labels.GitHubAPIError(404, "missing")
        if method == "POST" and endpoint == "repos/acme/repo/labels":
            raise claim_labels.GitHubAPIError(422, "already exists")

    monkeypatch.setattr(claim_labels, "_api", api)

    claim_labels.add_claimed_label("acme/repo#17")

    assert calls == [
        ("GET", "repos/acme/repo/issues/17"),
        ("GET", "repos/acme/repo/labels/claimed"),
        ("POST", "repos/acme/repo/labels"),
        ("GET", "repos/acme/repo/labels/claimed"),
        ("POST", "repos/acme/repo/issues/17/labels"),
    ]


def test_add_does_not_label_pull_requests(monkeypatch):
    calls = []

    def api(method, endpoint, **kwargs):
        calls.append((method, endpoint))
        if endpoint == "repos/acme/repo/issues/17":
            return {"number": 17, "pull_request": {"url": "https://example.test"}}
        if method == "GET":
            return [[{"name": "claimed"}]]

    monkeypatch.setattr(claim_labels, "_api", api)

    claim_labels.add_claimed_label("acme/repo#17")

    assert calls == [
        ("GET", "repos/acme/repo/issues/17"),
        ("GET", "repos/acme/repo/issues/17/labels?per_page=100"),
        ("DELETE", "repos/acme/repo/issues/17/labels/claimed"),
    ]


def test_remove_is_idempotent_and_leaves_other_labels(monkeypatch):
    calls = []

    def api(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        if method == "GET":
            return [[{"name": "bug"}]]

    monkeypatch.setattr(claim_labels, "_api", api)

    claim_labels.remove_claimed_label("acme/repo#17")

    assert calls == [
        ("GET", "repos/acme/repo/issues/17/labels?per_page=100", {"paginate": True})
    ]


def test_remove_deletes_only_the_claimed_label(monkeypatch):
    calls = []

    def api(method, endpoint, **kwargs):
        calls.append((method, endpoint))
        if method == "GET":
            return [[{"name": "bug"}, {"name": "claimed"}]]

    monkeypatch.setattr(claim_labels, "_api", api)

    claim_labels.remove_claimed_label("acme/repo#17")

    assert calls == [
        ("GET", "repos/acme/repo/issues/17/labels?per_page=100"),
        ("DELETE", "repos/acme/repo/issues/17/labels/claimed"),
    ]


def test_list_claimed_issues_includes_pull_requests_and_sorts(monkeypatch):
    def api(method, endpoint, **kwargs):
        assert method == "GET"
        assert kwargs["paginate"] is True
        return [
            [{"number": 19}, {"number": 4, "pull_request": {"url": "x"}}],
            [{"number": 3}, {"number": 19}],
        ]

    monkeypatch.setattr(claim_labels, "_api", api)

    assert claim_labels.list_claimed_issues("acme", "repo") == [3, 4, 19]


def test_claim_race_and_redis_outage_do_not_add_label(
    redis_port, flush_redis, closed_port, monkeypatch
):
    added = []
    monkeypatch.setattr(claim_labels, "add_claimed_label", added.append)
    claims.claim("acme/repo#17", "first", **_kw(redis_port))

    with pytest.raises(claims.ClaimHeld):
        claim_labels.claim("acme/repo#17", "second", **_kw(redis_port))
    with pytest.raises(slots.CoordinatorUnreachable):
        claim_labels.claim(
            "acme/repo#18", "second", redis_host="127.0.0.1", redis_port=closed_port
        )

    assert added == []
    assert claims.claims_for(["acme/repo"], **_kw(redis_port))["acme/repo#17"]["session"] == "first"


def test_claim_adds_label_after_redis_claim(redis_port, flush_redis, monkeypatch):
    target = "acme/repo#17"
    connection = _kw(redis_port)
    added = []

    def add_label(item):
        claim = claims.claims_for(["acme/repo"], **connection)[target]
        assert claim["session"] == "worker"
        added.append(item)

    monkeypatch.setattr(claim_labels, "add_claimed_label", add_label)

    claim_labels.claim(target, "worker", **connection)

    assert added == [target]



def test_delayed_label_add_after_release_removes_label_and_index(
    redis_port, flush_redis, monkeypatch
):
    target = "acme/repo#17"
    connection = _kw(redis_port)
    labeled = False

    def delayed_add(item):
        nonlocal labeled
        claim_labels.release_claim(item, "worker", **connection)
        labeled = True

    def remove_label(_item):
        nonlocal labeled
        labeled = False

    monkeypatch.setattr(claim_labels, "add_claimed_label", delayed_add)
    monkeypatch.setattr(claim_labels, "remove_claimed_label", remove_label)

    assert claim_labels.claim(target, "worker", **connection) is True

    assert claims.claims_for(["acme/repo"], **connection) == {}
    assert labeled is False
    assert claims._indexed_targets(**connection) == set()


def test_label_add_failure_rolls_back_new_redis_claim(redis_port, flush_redis, monkeypatch):
    def fail(_target):
        raise claim_labels.GitHubAPIError(403, "write denied")

    monkeypatch.setattr(claim_labels, "add_claimed_label", fail)

    with pytest.raises(claim_labels.ClaimLabelSyncError, match="Redis claim was released"):
        claim_labels.claim("acme/repo#17", "worker", **_kw(redis_port))

    assert claims.claims_for(["acme/repo"], **_kw(redis_port)) == {}
    assert claims._indexed_targets(**_kw(redis_port)) == {"acme/repo#17"}


def test_failed_initial_label_sync_does_not_release_successful_same_holder_retry(
    redis_port, flush_redis, monkeypatch
):
    target = "acme/repo#17"
    connection = _kw(redis_port)
    calls = 0

    def add_label(_target):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert claim_labels.claim(target, "worker", **connection) is False
            raise claim_labels.GitHubAPIError(403, "write denied")

    monkeypatch.setattr(claim_labels, "add_claimed_label", add_label)

    with pytest.raises(claim_labels.ClaimLabelSyncError):
        claim_labels.claim(target, "worker", **connection)

    assert calls == 2
    assert claims.claims_for(["acme/repo"], **connection)[target]["session"] == "worker"


def test_failed_initial_label_sync_preserves_concurrent_holder_renewal(
    redis_port, flush_redis, monkeypatch
):
    target = "acme/repo#17"
    connection = _kw(redis_port)
    calls = 0
    added = []

    def add_label(item):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert claim_labels.renew_holder_claims("worker", **connection) == [
                target
            ]
            raise claim_labels.GitHubAPIError(403, "write denied")
        added.append(item)

    monkeypatch.setattr(claim_labels, "add_claimed_label", add_label)

    with pytest.raises(claim_labels.ClaimLabelSyncError):
        claim_labels.claim(target, "worker", **connection)

    assert calls == 2
    assert added == [target]
    assert claims.claims_for(["acme/repo"], **connection)[target]["session"] == "worker"


def test_repeated_claim_label_failure_keeps_existing_claim(
    redis_port, flush_redis, monkeypatch
):
    target = "acme/repo#17"
    connection = _kw(redis_port)
    claims.claim(target, "worker", **connection)

    def fail(_target):
        raise claim_labels.GitHubAPIError(403, "write denied")

    monkeypatch.setattr(claim_labels, "add_claimed_label", fail)

    with pytest.raises(
        claim_labels.ClaimLabelSyncError, match="existing Redis claim remains active"
    ):
        claim_labels.claim(target, "worker", **connection)

    assert claims.claims_for(["acme/repo"], **connection)[target]["session"] == "worker"


def test_renewal_label_failure_is_reported_and_claim_stays_active(
    redis_port, flush_redis, monkeypatch
):
    target = "acme/repo#17"
    claims.claim(target, "worker", **_kw(redis_port))

    def fail(_target):
        raise claim_labels.GitHubAPIError(403, "write denied")

    monkeypatch.setattr(claim_labels, "add_claimed_label", fail)

    with pytest.raises(claim_labels.ClaimLabelSyncError, match="claim remains active"):
        claim_labels.renew_claim(target, "worker", **_kw(redis_port))

    assert claims.claims_for(["acme/repo"], **_kw(redis_port))[target]["session"] == "worker"


def test_renewal_reapplies_missing_claimed_label(redis_port, flush_redis, monkeypatch):
    target = "acme/repo#17"
    connection = _kw(redis_port)
    added = []
    claims.claim(target, "worker", **connection)
    monkeypatch.setattr(claim_labels, "add_claimed_label", added.append)

    assert claim_labels.renew_claim(target, "worker", **connection) is True

    assert added == [target]


def test_holder_renewal_reapplies_label_for_each_claim(
    redis_port, flush_redis, monkeypatch
):
    connection = _kw(redis_port)
    targets = {"acme/repo#17", "acme/repo#18"}
    added = []
    for target in targets:
        claims.claim(target, "worker", **connection)
    monkeypatch.setattr(claim_labels, "add_claimed_label", added.append)

    renewed = claim_labels.renew_holder_claims("worker", **connection)

    assert set(renewed) == targets
    assert set(added) == targets


def test_wrong_holder_release_does_not_change_label(redis_port, flush_redis, monkeypatch):
    removed = []
    target = "acme/repo#17"
    claims.claim(target, "worker", **_kw(redis_port))
    monkeypatch.setattr(claim_labels, "remove_claimed_label", removed.append)

    assert claim_labels.release_claim(target, "other", **_kw(redis_port)) is False
    assert removed == []
    assert target in claims.claims_for(["acme/repo"], **_kw(redis_port))


def test_release_keeps_label_when_another_claim_wins_the_race(
    redis_port, flush_redis, monkeypatch
):
    target = "acme/repo#17"
    connection = _kw(redis_port)
    added = []
    removed = []
    claims.claim(target, "first", **connection)
    real_release = claims.release_claim

    def release_then_reclaim(item, holder, **kwargs):
        released = real_release(item, holder, **kwargs)
        if released:
            claims.claim(item, "second", **kwargs)
        return released

    monkeypatch.setattr(claims, "release_claim", release_then_reclaim)
    monkeypatch.setattr(claim_labels, "add_claimed_label", added.append)
    monkeypatch.setattr(claim_labels, "remove_claimed_label", removed.append)

    assert claim_labels.release_claim(target, "first", **connection) is True

    assert added == [target]
    assert removed == []
    assert claims.claims_for(["acme/repo"], **connection)[target]["session"] == "second"


def test_release_keeps_label_until_case_variant_claim_is_released(
    redis_port, flush_redis, monkeypatch
):
    connection = _kw(redis_port)
    targets = ("Acme/Repo#17", "acme/repo#17")
    labeled = False
    removed = []

    def add_label(_target):
        nonlocal labeled
        labeled = True

    def remove_label(target):
        nonlocal labeled
        labeled = False
        removed.append(target)

    monkeypatch.setattr(claim_labels, "add_claimed_label", add_label)
    monkeypatch.setattr(claim_labels, "remove_claimed_label", remove_label)
    for target, holder in zip(targets, ("holder-a", "holder-b")):
        claim_labels.claim(target, holder, **connection)

    assert labeled is True
    assert claim_labels.release_claim(targets[0], "holder-a", **connection) is True
    assert labeled is True
    assert removed == []

    assert claim_labels.release_claim(targets[1], "holder-b", **connection) is True
    assert labeled is False
    assert removed == [targets[1]]


def test_release_label_failure_is_not_reported_as_fully_synced(
    redis_port, flush_redis, monkeypatch
):
    target = "acme/repo#17"
    claims.claim(target, "worker", **_kw(redis_port))

    def fail(_target):
        raise claim_labels.GitHubAPIError(403, "write denied")

    monkeypatch.setattr(claim_labels, "remove_claimed_label", fail)

    with pytest.raises(claim_labels.ClaimLabelSyncError, match="released the Redis claim"):
        claim_labels.release_claim(target, "worker", **_kw(redis_port))

    assert claims.claims_for(["acme/repo"], **_kw(redis_port)) == {}
    assert claims._indexed_targets(**_kw(redis_port)) == {target}


def test_release_redis_failure_is_not_mislabeled_as_github_failure(monkeypatch):
    def fail_index(*args, **kwargs):
        raise slots.CoordinatorUnreachable("claim index")

    monkeypatch.setattr(claims, "release_claim", lambda *args, **kwargs: True)
    monkeypatch.setattr(claims, "_mark_label_index_if_active", fail_index)

    with pytest.raises(slots.CoordinatorUnreachable, match="claim index"):
        claim_labels.release_claim("acme/repo#17", "worker")


def test_reconcile_repairs_active_label_and_removes_expired_claim_label(
    redis_port, flush_redis, monkeypatch
):
    connection = _kw(redis_port)
    claims.claim("acme/repo#17", "active", **connection)
    claims.claim("acme/repo#18", "expired", ttl=0.05, **connection)
    time.sleep(0.1)

    labeled = {17, 18}
    added = []
    removed = []
    monkeypatch.setattr(claim_labels, "list_claimed_issues", lambda owner, repo: sorted(labeled))
    monkeypatch.setattr(
        claim_labels,
        "add_claimed_label",
        lambda target: (added.append(target), labeled.add(int(target.rsplit("#", 1)[1]))),
    )
    monkeypatch.setattr(
        claim_labels,
        "remove_claimed_label",
        lambda target: (removed.append(target), labeled.discard(int(target.rsplit("#", 1)[1]))),
    )
    monkeypatch.setattr(reconcile.quest, "load_quests", lambda repos, **kwargs: ([], []))
    monkeypatch.setattr(reconcile, "_release_quest_focuses", lambda *args, **kwargs: [])
    monkeypatch.setattr(reconcile, "_release_started_quests", lambda *args, **kwargs: [])

    lines, warnings = reconcile.reconcile([], connection, code_dir="/repos", label_repos=[])

    assert lines == []
    assert warnings == []
    assert labeled == {17}
    assert added == ["acme/repo#17"]
    assert removed == ["acme/repo#18"]
    assert claims._indexed_targets(**connection) == {"acme/repo#17"}


def test_reconcile_keeps_active_label_when_local_repo_case_differs(
    redis_port, flush_redis, monkeypatch
):
    connection = _kw(redis_port)
    target = "Acme/Repo#17"
    claims.claim(target, "worker", **connection)
    labeled = {17}
    listed = []
    added = []
    removed = []

    monkeypatch.setattr(
        reconcile.roadmap,
        "_repo_identity",
        lambda path: ("acme", "repo", None),
    )

    def list_claimed(owner, repo):
        listed.append((owner, repo))
        return sorted(labeled)

    def add_label(item):
        added.append(item)
        labeled.add(int(item.rsplit("#", 1)[1]))

    def remove_label(item):
        removed.append(item)
        labeled.discard(int(item.rsplit("#", 1)[1]))

    monkeypatch.setattr(claim_labels, "list_claimed_issues", list_claimed)
    monkeypatch.setattr(claim_labels, "add_claimed_label", add_label)
    monkeypatch.setattr(claim_labels, "remove_claimed_label", remove_label)

    warnings = reconcile._sync_claim_labels(["repo"], connection, code_dir="/repos")

    assert warnings == []
    assert listed == [("Acme", "Repo")]
    assert added == [target]
    assert removed == []
    assert labeled == {17}
    assert claims._indexed_targets(**connection) == {target}


def test_reconcile_keeps_active_labels_when_targets_use_mixed_repo_case(
    redis_port, flush_redis, monkeypatch
):
    connection = _kw(redis_port)
    targets = ["Acme/Repo#17", "acme/repo#18"]
    for number, target in zip((17, 18), targets):
        claims.claim(target, f"worker-{number}", **connection)
    labeled = {17, 18}
    listed = []
    added = []
    removed = []

    monkeypatch.setattr(
        reconcile.roadmap,
        "_repo_identity",
        lambda path: ("acme", "repo", None),
    )

    def list_claimed(owner, repo):
        listed.append((owner, repo))
        return sorted(labeled)

    def add_label(target):
        added.append(target)
        labeled.add(int(target.rsplit("#", 1)[1]))

    def remove_label(target):
        removed.append(target)
        labeled.discard(int(target.rsplit("#", 1)[1]))

    monkeypatch.setattr(claim_labels, "list_claimed_issues", list_claimed)
    monkeypatch.setattr(claim_labels, "add_claimed_label", add_label)
    monkeypatch.setattr(claim_labels, "remove_claimed_label", remove_label)

    warnings = reconcile._sync_claim_labels(["repo"], connection, code_dir="/repos")

    assert warnings == []
    assert listed == [("acme", "repo")]
    assert added == ["acme/repo#17", "acme/repo#18"]
    assert removed == []
    assert labeled == {17, 18}
    assert claims._indexed_targets(**connection) == set(targets)


def test_reconcile_removes_unindexed_label_from_local_repo(
    redis_port, flush_redis, monkeypatch
):
    connection = _kw(redis_port)
    labeled = {19}
    removed = []
    monkeypatch.setattr(reconcile.roadmap, "_repo_identity", lambda path: ("acme", "repo", None))
    monkeypatch.setattr(claim_labels, "list_claimed_issues", lambda owner, repo: sorted(labeled))
    monkeypatch.setattr(
        claim_labels,
        "remove_claimed_label",
        lambda target: (removed.append(target), labeled.discard(int(target.rsplit("#", 1)[1]))),
    )
    monkeypatch.setattr(reconcile.quest, "load_quests", lambda repos, **kwargs: ([], []))
    monkeypatch.setattr(reconcile, "_release_quest_focuses", lambda *args, **kwargs: [])
    monkeypatch.setattr(reconcile, "_release_started_quests", lambda *args, **kwargs: [])

    lines, warnings = reconcile.reconcile(
        [], connection, code_dir="/repos", label_repos=["repo"]
    )

    assert lines == []
    assert warnings == []
    assert labeled == set()
    assert removed == ["acme/repo#19"]


def test_release_removes_label_and_clears_index(redis_port, flush_redis, monkeypatch):
    target = "acme/repo#17"
    connection = _kw(redis_port)
    removed = []
    claims.claim(target, "worker", **connection)
    monkeypatch.setattr(claim_labels, "remove_claimed_label", removed.append)

    assert claim_labels.release_claim(target, "worker", **connection) is True

    assert removed == [target]
    assert claims._indexed_targets(**connection) == set()
