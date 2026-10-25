---
name: triage
description: >-
  Prioritize GitHub issues and choose the next work to delegate. Use this
  skill when sorting a backlog, applying issue labels, checking dependencies,
  or preparing an issue for Lupin routing. Please delegate this to an economical model.
compatibility: Requires Lupin and GitHub CLI. Redis is required for fleet claims.
---

# Triage issues with Lupin

Read the repository's `AGENTS.md` and delegation notes first. Read the latest
issue comments before making a decision. An issue body can be out of date.

## Read the queue

From a configured repository checkout, run:

```sh
lupin roadmap --repo OWNER/REPO --stage all --json
lupin roadmap --dag --json
lupin review-route --prefetch 123 --repo OWNER/REPO
```

`roadmap` ranks open work. `roadmap --dag` shows issue dependencies across
enabled repositories. `review-route --prefetch` fetches issue or pull request
text and comments; it does not choose a model. For `roadmap`, add `--repo
OWNER/REPO` to read one repository. Omit it to read every enabled repository.

Use the repository's label map if it has one. Keep its priority, size, type,
and blocker labels. Use a separate `claimed` label for active work. Add it
during repository setup if it does not exist. If you cannot add it, post the
claim comment and report that the label is missing. Lupin does not change
GitHub labels.

## Label UI pressure-test work

Use `ui-pressure-test` for an issue whose main goal is a human-like UI
pressure test, when the repo's label map supports it. This is a work label,
not a priority, size, issue type, or status. Do not apply it to every bug or
design ticket found by a test. If the label is missing, report the gap; do not
create a tracker label during triage.

For Lupin's benchmark category, use `frontend-ui` for visual and interaction
testing. Do not create a separate `visual` category. For CAD or 3D modeling
work, use `cad-spatial` for the evaluator lane that tests geometry or spatial
tools. These categories describe the work; they do not replace the
`ui-pressure-test` work label.

Lupin's classifier returns one category and checks CAD before UI. A mixed
issue with CAD and UI terms routes as `cad-spatial`. Split visual and CAD work
into evaluator lanes, or route each lane separately. Do not assume one
benchmark category covers both.

## Decide what is ready

- Dispatch the PR work first in each cycle. Then use the free worker slots for
  new issues. See "Pull requests come first" in the `delegation-loop` skill.
- Check the dependency graph and the latest comments. Do not dispatch an issue
  with an open blocker.
- Check that the issue has a clear result. For a large issue, split it into
  small issues with separate acceptance checks before implementation.
- Check likely files before running issues in parallel. Treat Lupin's batch
  display as an estimate: it uses file paths written in issue text and does
  not confirm that a worker is running.
- Look for trivial or small issues (for example, `size-xs` or `size-s`) that
  touch the same likely files or one feature. Recommend one dispatch only when
  one worker can meet each issue's acceptance checks in one branch. Keep
  claimed, blocked, or already-in-PR issues separate.
- Report each recommendation under `Bundles:` as `#N + #M — reason`. Use
  `Bundles: none.` when no safe bundle fits. A bundle is one dispatch, not a
  duplicate; the orchestrator decides whether to use it.
- Treat priority, size, and status labels as repository data. Common names are
  `P0` to `P3`, `size-xs` to `size-xl`, `bug`, `needs-expert-decision`,
  `completely-blocked-on-human`, and `epic`. A repository may use other names.
- `needs-expert-decision` is a routing signal, not an automatic stop. Ask a
  capable reviewer to resolve the decision. Stop for a person only when the
  task needs their credentials, money, hardware, or another approval that the
  repository rules reserve for them.
- Use `completely-blocked-on-human` only for a real human-only blocker. Record
  the exact missing action. Work on other issues while it is blocked.

## Check for duplicate issues

Before you write the `Duplicates` list, check each pair of similar issues.

1. Read both issue bodies and comments.
2. Check both issues' labels, including `claimed`.
3. Check for open pull requests that reference either issue.
4. Choose one verdict:
   - `duplicate`: The issues ask for the same change. Name the issue to keep.
   - `overlap`: The issues have different scopes. Name both and state the split.
   - `not duplicate`: The issues ask for different changes. Give one reason.

If either issue has the `claimed` label, use `overlap` or `not duplicate`. Use
`duplicate` only if the owner confirms it.

Write the `Duplicates` list in your reply to the orchestrator. Begin it with a
line that reads `Duplicates:`. Then write one line per pair:
`#N ~ #M — verdict: reason.` If no pair is similar, write `Duplicates: none.`

Close the duplicates.

## Writes that triage may make

Triage may make these writes on the issues it reads:

- **Labels.** Apply the repository's priority label and size label to each open
  issue that does not have them. Apply the type and blocker labels from the
  repository's label map when they apply. Do this when you decide the issue's
  priority and size. Do not remove a label that a person set.
- **Native blocked-by links.** Add a link when you find that one issue must
  finish before another. Add it in GitHub's issue dependency feature. Do this
  when you check dependencies.
- **Comments.** Post a comment only to record a decision that a later reader
  needs, for example the reason for a blocker. Start it with `Loop:`.

Please also group / structure issues into epics, mark issues as sub-issues, etc. 

You may close an addressed issue during triage. You may revise an issue's title or body.
Claim labels and claim comments belong to the claim step below.

A dependency that is only text ("Depends on #N") does not appear in
`lupin roadmap --dag`. That command reads native blocked-by links only. Treat a
text dependency as a blocker when you decide what to dispatch. Then add the
native link, so `--dag` and `--stage blocked` show it.

## Route and reserve the issue

If you are prepared to route and reserve the issue: 
From the issue's repository checkout, `lupin place 123 --json` classifies the
issue, recommends a model and effort, and ranks available machines. It does
not claim the issue or start an agent. `lupin quota` displays the shared Redis
snapshot and refreshes it with local provider data when this machine has
credentials. `route()` reads quota from the local machine, so the two results
may differ.

If Lupin cannot place the task, use `lupin review-route` with the issue's
category and size. Do not ignore a reported quota wait by selecting the
blocked model yourself.

When Redis fleet claims are configured, reserve the issue before dispatch:

```sh
lupin claim OWNER/REPO#123 --holder SESSION
lupin renew-claim OWNER/REPO#123 --holder SESSION
lupin release-claim OWNER/REPO#123 --holder SESSION
```

A claim is stored in Redis. It does not add a GitHub label or comment, and it
does not store the worktree path. After the claim succeeds, the agent that made
the claim adds the `claimed` label and a comment with the machine, worktree,
branch, and holder. When the claim ends, remove the label and comment with the
result.
Start each comment with `Loop:`.
Do not mark an issue as claimed if the Redis claim fails. Lupin has no local
claim fallback.

For a related set of issues, `lupin quest start --issue 123 --issue 124`
claims the issues and registers a quest on a selected machine. It does not
start an agent or disable quota pacing. Do not claim those issues again.

## Handoff

Finish with a a short brief:

- Issue number, goal, and acceptance checks.
- Current comments and open dependencies.
- Likely files and known parallel work.
- Lupin's model, effort, and machine recommendation.
- Claim or quest ID, machine, worktree path, and branch.
- For a worker in a Herdr pane: agent name, workspace ID, pane ID, tab ID,
  and cwd. Read them from `herdr agent list`.
- Repository instructions and required verification.

Tell the worker to use `/ship`. Record dispatches, handoffs, and split links
in the shared Redis ledger:

```sh
lupin ledger append OWNER/REPO --event dispatch --issue N \
  --status running --branch BRANCH
lupin ledger append OWNER/REPO --event handoff --issue N --status STATUS \
  --summary TEXT --highlights TEXT --evidence TEXT --decisions TEXT --next TEXT
lupin ledger read OWNER/REPO --json
```

Add `--child N` for each split issue. If Redis is unavailable, ledger commands
exit 3. Write `.loop/loop-state.json` only as the `handoff` skill describes.
