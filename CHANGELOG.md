# Changelog

All notable changes to Lupin. The version has one source:
`pyproject.toml`. Add one line under `Unreleased` in each pull request.

## Unreleased

### Added

- `lupin logs` shows events from a loop run. Lupin keeps up to 20 logs per
  repo and removes logs older than 30 days.
- `lupin --version` prints the package version. The flake reads the same
  version from `pyproject.toml`.
- `docs/release.md` gives the release steps.
- The Roadmap board shows stage tabs with counts, like the List view.
- The triage skill checks issue details and linked open PRs before it reports
  likely duplicates.
- Running loops save a quota handoff before the active provider runs out. A
  pending handoff cancels the slot wait, so the low-quota agent does not start.
  Loops resume in the same workspace with a configured provider that has fresh
  quota.

### Changed

- `lupin run` starts from `origin/HEAD`, or else `origin/main`. It no longer
  checks `origin/release/next` or `origin/master`, and CI no longer lists
  `release/next`.

### Fixed

- The Roadmap page family uses one set of stage words (In progress, Ready,
  Later, Blocked) on the board, the list, the detail panel and the queue
  graph. The empty states keep the selected repositories visible, the note
  under the board uses plain words, the priority filter offers P4, and the
  epics footer and the pager read the same way when the list is empty.
- Roadmap repo-fetch warnings can be dismissed on Board and List. A dismissal
  stays hidden after a reload and applies to one repo and warning.
- `lupin roadmap` text output shows one line per repo for issues with no
  priority label. `--json` still lists each issue. The triage and
  delegation-loop skills name skills in one form, and triage defines the
  `Duplicates` reply.

### Fixed

- New loop prompts include saved repo instructions when the worktree copy
  is missing or different.
- "Run now" passes dash-leading notes as one argument, and rejects notes
  with a NUL byte before dispatch.
- Routing and loop profiles block Claude's weekly quota above 90%. A blocked
  route tries only same- or lower-tier models with verified prices no higher
  than the blocked model, then defers with a reason.
- OMP prompt jobs do not wait for caller stdin to close.
- Claim reads use Redis `TTL` for the remaining time.
- `lupin place` reports quota deferrals when no reset time is available.

## 0.1.0

- First version. Model routing, slot leases, the dashboard, the fleet
  registry, claims, quests, the loop runtime, the supervisor, and debriefs.
