---
name: delegation-loop
description: >-
  Coordinate an issue backlog with Lupin. Use this skill when starting or
  continuing an issue loop, selecting work for agents, watching a fleet, or
  handing off a loop run.
compatibility: >-
  Requires Lupin, GitHub CLI, an agent runner, and the `delegation-loop`,
  `triage`, `ship`, `code-review`, and `handoff` skills on each worker host. Redis is
  required for fleet claims.
---

# Run a Lupin delegation loop

Read the root `AGENTS.md` first. If `docs/delegation-loop.md` exists, read
it; Lupin can run without it. Follow the repo's branch, worktree, test, and
release rules. Read the latest issue comments.

A loop pushes its branch to upstream, the `origin` remote (`OWNER/REPO`). If
upstream denies the push, it pushes to the existing fork, the `fork` remote. It
opens the PR on upstream, against `main`. Never create a new fork. Never
open a PR or an issue on a fork.

Use an isolated worktree for each worker. Do not let parallel workers edit the
same checkout. A worker that opens a new branch creates its worktree from
upstream `main`:

```sh
git fetch origin
git worktree add <path> -b <branch> origin/main
```

On a Herdr worker, make the worktree as "Dispatch work" says instead.

Keep each worktree under `.claude/worktrees/`. Record the machine, worktree
path, branch, and issue in the dispatch and the repository's handoff record.
When a worker runs in a Herdr pane, record its agent name, workspace ID, pane
ID, tab ID, and cwd.
`herdr agent list` reports all of them. See "Talk to an agent in a Herdr
pane" below.

## Pull requests come first

Get pull requests (PRs) approved and merged first. This is the top priority.
Do it before you start work on any new issue.

A PR that waits goes stale. `main` gets new commits. The PR branch no longer
contains the current `main`, so the PR fails a merge gate. A sync (a merge of
the current `main` into the PR branch) fixes that. A sync changes the head SHA
(the newest commit ID on the PR branch). The approval then no longer covers it,
and the PR needs a new review. Merge an approved PR at once. The orchestrator
does not wait for the owner when all four merge gates pass.

At the start of every cycle, the orchestrator handles the open PRs into
`main`. The orchestrator is the agent that dispatches and merges work.
It does this before it reads the backlog. Ignore draft PRs. Steps 2 and 3 are
only for a PR that the loop opened. The loop opened a PR when the ledger has a
dispatch event for its branch. Read all the events:

```sh
lupin ledger read OWNER/REPO --limit 100000 --json
```

For each PR that is not a draft, do the first step that applies:

1. The PR passes the four merge gates: merge it. See "Continuous merge of
   approved PRs".
2. The PR is approved at its current head SHA and is behind `main`:
   dispatch a worker to sync it. A sync changes the head SHA, so get a new
   approval. If the sync stops on conflicts, the worker did not fail. Dispatch
   a fix worker to resolve them. Do not repeat that sync.
3. The PR has review findings or a failed repo gate at its current head SHA:
   dispatch a fix worker. The repo gate is the set of checks that `AGENTS.md`
   defines. Give the worker the findings or the gate output. The fix goes on
   the same branch.
4. The PR has no review at its current head SHA: dispatch a reviewer. Do not
   wait for the worker to request a review.
5. The PR waits for a gate that is still running: wait for the result.

A worker is a fix worker, a sync worker, or a reviewer. Do not dispatch a
worker for a PR that already has one running. Do not push to the branch of a PR
that the loop did not open.

A sync worker or a fix worker starts from the PR branch. Give it the remote and
the branch name. The remote is `origin` or `fork`, the one that holds the PR
branch. The first three commands below make its worktree. The last one pushes
its commits:

```sh
git fetch origin
git fetch <remote> <branch>
git worktree add --detach <path> <remote>/<branch>
git push <remote> HEAD:<branch>
```

A sync worker or a fix worker uses these commands, not the other worktree and
push commands in this skill. On a Herdr worker it also opens the worktree in
Herdr, as "Dispatch work" says. A detached worktree does not touch a local
branch that has commits the remote lacks.

A PR is approved at a head SHA in one case. A submitted review has the state
`APPROVED`, and its `commit_id` is that full SHA. A review of an older SHA does
not count. A PR comment that says "approve" does not count. Read the reviews:

```sh
gh api --paginate repos/OWNER/REPO/pulls/<number>/reviews
```

Post a verdict as a review, through the API, with the SHA that you read.
`gh pr review` binds the review to the head at the time of posting, and the head
can move first:

```sh
gh api repos/OWNER/REPO/pulls/<number>/reviews \
  -f event=APPROVE -f commit_id=<full SHA> -F body=@verdict.md
```

Use `-f event=REQUEST_CHANGES` when the review asks for changes.

A PR is blocked on an owner decision when one of these is true:

- It needs a sync or a fix, and the loop did not open it.
- A worker for it fails. A worker fails when it ends without the push or the
  report that its brief asks for. A worker also fails when its report says that
  it could not do the work. A sync worker that stops on conflicts does not
  fail. See step 2.
- A worker for it stops to ask the owner.
- A fix worker ends and the gate still fails.
- Its merge fails.

Report each blocked PR in the handoff, with its head SHA. Skip it. On each
cycle, read the new comments on each blocked PR. Retry the PR when its head
SHA changes or the owner asks in a comment.

A PR is pending when it is open, is not a draft, is not merged, and is not
blocked. Do not dispatch work on a new issue while a PR into `main` is
pending. Work that is already running continues. A reviewer, a sync worker, or
a fix worker for a pending PR is not new work.

## Install skills on loop hosts

The `skills/` tree is the source. Installing the Lupin command does not
install these files. A Nix host flake maps the five required directories from
its pinned Lupin input into each runner's skill set. A rebuild creates or
updates the links; do not copy the files by hand. The `ui-pressure-test`
skill is optional. Add it to a selected runner when a task needs visual QA.

- Claude Code: `~/.claude/skills/`
- Pi: `~/.pi/agent/skills/`
- OMP: `~/.omp/agent/skills/`

Add all five required names to each host's skill set, including any curated
subset. Add `ui-pressure-test` to hosts that run UI pressure tests. After a
rebuild, check that every required `SKILL.md` is readable:

```sh
for root in "$HOME/.claude/skills" "$HOME/.pi/agent/skills" "$HOME/.omp/agent/skills"; do
  for name in delegation-loop triage ship code-review handoff; do
    test -r "$root/$name/SKILL.md" || {
      echo "Missing $root/$name/SKILL.md" >&2
      exit 1
    }
  done
done
```

Start a new agent session and confirm it loads `/delegation-loop`, `/triage`,
`/ship`, `/code-review`, and `/handoff`. If the task needs UI pressure testing, also
confirm it loads `/ui-pressure-test`. Do not start a worker if a skill it
needs is missing.

## Open pull requests come first

Review every open PR before you start issue work. Check them again after
every merge and after every worker report.

1. List them, drafts included:

   ```sh
   gh pr list --repo OWNER/REPO --state open --limit 200 \
     --json number,headRefOid,isDraft,mergeable
   ```

2. Each PR must be in one of two states:
   - **Approved.** A submitted review has the state APPROVED, and its
     `commit_id` is the full SHA of the current head. A review of an older
     SHA does not count. A PR comment that says "approve" does not count.
     Read the reviews with
     `gh api --paginate repos/OWNER/REPO/pulls/N/reviews`.
   - **Worked.** A reviewer or a fixer runs on the current head. Check that
     the worker still runs: use `kill -0` on its saved PID, `ListAgents`, or
     `herdr agent list`. A new commit shows that work happened. It does not
     show that the worker still runs. A ledger "dispatch" entry is not proof.
3. A PR in neither state is idle. Dispatch a reviewer now if no review covers
   the current head. Dispatch a fixer now if the PR has a conflict with its
   base branch (`mergeable` is `CONFLICTING`) or an open finding. An approved
   PR with a conflict needs a fixer too. A comment that says "approve" is not
   a review. Dispatch a reviewer for that head. Post its verdict as a review
   (step 6).
4. PR work comes before issue work. If the slots are full, stop the
   lowest-priority issue job first. One reviewer may cover several small
   PRs. If every slot holds PR work and the machine has room (`free -h`),
   run one more job. A failing gate on the base branch blocks only the
   merge. It does not block a review or a fix.
5. A new head voids the old approval. A merge of the base branch into the PR
   branch makes a new head. Dispatch a new review at once. It may cover only
   the files that the new commits changed.
6. Post a review through the API, with the SHA that you read. `gh pr review`
   uses the head at the time of posting, and the head can move first:

   ```sh
   pr_number=N
   head_sha=FULL_SHA_THAT_YOU_READ
   gh api "repos/OWNER/REPO/pulls/${pr_number}/reviews" \
     -f event=APPROVE -f commit_id="$head_sha" -F body=@verdict.md
   ```

   Use `-f event=REQUEST_CHANGES` when the review asks for changes.

## Read the backlog

Read the backlog only after you handle the open PRs. See "Pull requests come
first".

From a configured checkout, run:

```sh
lupin roadmap --stage all --json
lupin roadmap --dag --json
lupin review-route --prefetch 123 --repo OWNER/REPO
lupin place 123 --json
```

The roadmap ranks issues. The DAG view shows dependencies. Prefetch gets the
latest issue or pull request text and comments. Placement recommends a model,
effort, and machine. It does not claim work or start an agent.

Use `/triage` to decide what is ready. Read the latest comments and repo
instructions. Check likely files and live work before you run issues in
parallel. Run issues together only when their work does not overlap.

`lupin quota` displays the shared Redis snapshot and refreshes it with local
provider data when this machine has credentials. `route()` still reads quota
from the local machine, so the two results may differ.

## Claim and label work

Use the same target and holder to claim, renew, and release work. The target is
always `OWNER/REPO#N` with the full repo name. Never pass a bare number. Two
repos can each have a PR `#599`, and the claim key needs the repo to tell them
apart.

Issues and pull requests share one number count within a repo. So `#599` names
one item in that repo. The brief and the ledger still say which kind it is:
`issue` or `PR`.

Claim an issue when a worker starts its implementation. For a PR, the
orchestrator claims it before it dispatches a reviewer or a fix worker. It
releases the claim when that worker's result is posted or the worker stops.

```sh
lupin claim OWNER/REPO#123 --holder SESSION         # issue 123
lupin claim OWNER/REPO#599 --holder SESSION         # PR 599, same repo
lupin renew-claim OWNER/REPO#599 --holder SESSION
lupin release-claim OWNER/REPO#599 --holder SESSION
```

Exit code 2 from `claim` means another holder has the item. Read the holder in
the error text. Do not dispatch on that item. Exit code 3 means Redis is not
reachable. Do not dispatch on the item until it is reachable.

By default, Lupin expires a claim after 10 minutes. Use `--ttl SECONDS` to
change this limit. Renew every 2 minutes while work continues. Each renewal
starts the timer again. If the claim is not renewed, Redis removes it when the
timer ends. `lupin reconcile` runs once per call. It releases claims with no
renewal for 10 minutes, even when `--ttl` is longer. Its schedule sets the
cleanup delay.

The label and comment steps below apply to issue claims only. A PR claim has no
label. Record it in the ledger and in the PR verdict.

Lupin does not yet sync claims to GitHub. Issue #43 tracks automatic
`claimed` label updates. Until it ships, the claim owner must update the issue
manually. Use the repo's `claimed` label. Add it during repo setup if it does
not exist. If you cannot add it, post the comment and report that the label is
missing. Include the machine, worktree, branch, and holder:

```sh
gh issue edit 123 --repo OWNER/REPO --add-label claimed
gh issue comment 123 --repo OWNER/REPO \
  --body "Claimed: machine=HOST; worktree=PATH; branch=BRANCH; holder=SESSION"
```

When the claim ends, remove the label and post the result, including the PR
number or reason the work stopped:

```sh
gh issue edit 123 --repo OWNER/REPO --remove-label claimed
gh issue comment 123 --repo OWNER/REPO --body "Released: PR #123"
```

The claim owner makes these updates; workers must not post duplicate claim
comments. If the Redis claim fails, do not mark it as claimed. Follow the repo's
local coordination rule instead. If a lease expires or `lupin reconcile`
releases it, check that the GitHub label and comment also match.

A claim needs Redis. If Redis is not available, Lupin has no local claim
fallback. Do not report a Lupin claim as active.

For related issues, `lupin quest start --issue 123 --issue 124` claims and
orders the issues, then registers a quest on a machine. It does not start an
agent. Do not claim those issues again.

## Dispatch work

Do not dispatch a worker on a new issue until you handle the open PRs. See
"Pull requests come first". A reviewer, a sync worker, or a fix worker for an
open PR is not new work.

Give each worker a short brief with:

- Issue goal and acceptance checks.
- Latest comments and open dependencies.
- Likely files and known parallel work.
- Model, effort, and machine recommendation.
- Claim or quest ID.
- Machine, absolute worktree path, and branch.
- Required tests and smoke checks.

Tell implementation workers to follow `AGENTS.md`, rule 1 under "Pull requests,
review, and handoff". Do not repeat the `/ship` implementation checklist.

When an issue makes a major change to a web app's user interface or key user
journey, and a preview is ready, dispatch a separate QA task with
`ui-pressure-test`. Keep the test agent read-only with respect to source code,
and keep its browser data separate from implementation work. Ask for evidence,
prioritized tickets, and a list of untested items. Do not ask the test agent to
fix findings.
A worker that opens a new branch uses the runner's worktree isolation feature
when it has one. Otherwise, it creates a separate worktree and verifies its
working directory. On a Herdr worker, it uses the Herdr worktree commands:

```sh
herdr worktree list --cwd "$PWD"
git fetch origin
herdr worktree create --branch BRANCH --base origin/main --cwd "$PWD"
herdr worktree open --path PATH --cwd "$PWD"
```

A sync worker or a fix worker always makes its worktree with the git commands
in "Pull requests come first". It never uses `herdr worktree create`. On a
Herdr worker, it then runs `herdr worktree open --path PATH --cwd "$PWD"`.

`--trust-repository` gives Git trust for one command. Use it only after the
owner verified the repository. Do not use it as a retry for a failed worktree
command.

## Communicate with workers

Use the agent runner's prompt and message tools for its own workers. Lupin's
remote commands control loops. They do not send free-form messages.

### Talk to an agent in a Herdr pane

A Lupin loop runs in one Herdr session on its machine. That session is
`lupin-loops`. Every loop is one workspace in it, and every workspace has one
named agent in it. A worker that the runner starts itself runs in the runner's
own session instead.

Herdr is the only way to send a message to a running agent. Read the agent
name from `herdr agent list`; do not guess it. An agent name is valid only
while that agent is running.

```sh
herdr agent list                                   # read the name from the output
herdr agent prompt REPO_NAME "report progress" --wait --timeout 120000
herdr agent read REPO_NAME --source recent-unwrapped --lines 120
```

The timeout value is in milliseconds. Omit `--timeout` only for a wait that
can last a long time; a prompt without it can wait forever.

Use `--wait` for normal work. It returns when the agent is ready for input.
Add `--until blocked` only when you want to wait for a question or an
approval. Use `--source detection` only when you check why Herdr does not
recognize the agent.

Agent states:

|State|Meaning|What to do|
|---|---|---|
|`idle`|Ready for input|Send the next prompt|
|`done`|Ready for input|Send the next prompt. The CLI state decides this, not the badge in the app|
|`working`|The agent is busy|Wait or read output|
|`blocked`|The agent stopped on a question or an approval|Read the output, then ask the owner. Do not answer for the owner|
|`unknown`|Herdr cannot tell what the agent does|Not proof that it finished. Check the pane yourself|

A timeout does not prove the text arrived, and a timeout does not prove it
did not arrive. A prompt that reports a stall can also be delivered. Read the
agent before you send the text again.

To read output from a pane, or to run a test or a smoke check beside the
worker, use the pane commands. `pane run` sends one command, and
`pane wait-output` returns when the output matches:

```sh
herdr pane run <pane_id> "pytest -q"
herdr pane wait-output <pane_id> --match "passed" --timeout 120000
herdr pane read <pane_id> --source recent-unwrapped --lines 120
```

Address panes by pane ID, or with `--current` for your own pane. Never use
another client's focused pane.

To start a worker in a Herdr pane, make the pane first, then start the agent
in it. `agent start` needs a shell that waits at its prompt. It never makes,
splits, or moves a pane:

```sh
herdr pane split --current --direction right --cwd "$PWD" --no-focus
herdr agent start worker-1 --kind omp --pane <pane-id-from-above>
```

The kind must be an installed one. `herdr integration status` lists them. An
agent with a kind that Herdr does not know shows as `unknown`.

### Read or control a loop on another machine

```sh
herdr --machine MACHINE agent list
lupin peek REPO --machine MACHINE
lupin attach REPO --machine MACHINE
lupin stop REPO --machine MACHINE
lupin schedule --machine MACHINE
lupin pause --machine MACHINE
lupin resume --machine MACHINE
```

`peek` reads output. `attach` opens the loop terminal. The other commands
control the loop or its schedule.

`herdr --machine MACHINE` must come before the subcommand on every command,
including the discovery command. It cannot be used with `--session` or
`--remote`. It uses the saved machine profile and its default session, so it
does not reach the `lupin-loops` session. To reach the loops on another
machine, use `lupin attach REPO --machine MACHINE`.

Locally, address a loop with the session name:

```sh
herdr --session lupin-loops agent list
```

## Review and merge each pull request

Dispatch `/code-review` for every pull request, including docs-only changes.
Review the current PR diff, not only the issue or a worker's report. Re-fetch
the latest PR comments and reviews before merge.

A worker that opens a new branch pushes only that branch, with
`git push -u origin <branch>`. If its token cannot push upstream, it pushes to
the fork with `git push -u fork <branch>`. A sync worker or a fix worker pushes
as in "Pull requests come first".
It never pushes `main`. The PR base is `main` on upstream:

<!-- markdownlint-disable MD013 -->
```sh
gh pr create --repo OWNER/REPO --base main --title "<title>" --body "Closes #N"
```
<!-- markdownlint-enable MD013 -->

The PR body has `Closes #N`. GitHub closes the issue when the PR merges into
the default branch, `main`.

Never open a PR or an issue on a fork. The PR always lives upstream. From a fork
branch, add `--head FORK_OWNER:<branch>`. If neither push works, the worker
stops. It reports the branch name and commit range. Do not review or merge the
branch.

After the PR opens, report its number to the orchestrator.

Use `lupin review-route --category CATEGORY --size SIZE --mode separate` for
a reviewer recommendation. Compare it with the issue's implementation route
and the worker's model tier.

To save tokens, one reviewer may review several small, independent PRs in one
dispatch. Keep a separate verdict for each PR. Review complex or high-risk
changes alone. Keep visual or 3D reviews to one or two PRs per dispatch.

The reviewer must be capable of doing the issue and must not be below the
worker's model tier. Prefer a reviewer one tier higher when quota allows.
Never dispatch Fable without the user's approval. Use Opus sparingly because
it costs more.

If the review finds a problem, dispatch a `fix` worker with the exact finding.
Tell it to push as in "Pull requests come first", and update the same PR.
Review the latest PR commit.
Repeat until the reviewer approves the current head SHA. The head SHA is the
newest commit ID on the branch.
The orchestrator (the agent that dispatches and merges work) posts the verdict
and findings on the PR. It posts them as a review through the API, with the
reviewed SHA (see "Open pull requests come first").

Before a branch is merged, it must contain the current `main`. Fetch
`origin` and the remote that holds the PR branch. Then run this check.
`REMOTE` is `origin` or `fork`, the remote that holds the PR branch. `BRANCH`
is the PR branch name:

```sh
git fetch origin
git fetch REMOTE
git merge-base --is-ancestor origin/main REMOTE/BRANCH
```

Exit code 0 means the PR branch contains `main`. Then continue to the
merge rules below. Exit code 1 means it does not. For an open PR approved at its
current head SHA, the worker runs `git merge origin/main` on the PR
branch. If the merge has conflicts, report them and stop. The orchestrator then
dispatches a fix worker. It runs the same merge, resolves the conflicts, commits
the result, and pushes. If the merge has no conflicts, push it. Use
`git push <remote> HEAD:<branch>`. Run the check again. If it exits 0, continue
to the merge rules. A sync is not a merge of the pull request. Otherwise stop.
Do not rebase. Any other exit code from the first check means the check failed.
Report it and stop. Do not merge.

An approval binds to the head SHA. If the sync changed the head, get a new
approval at the new head before you merge. The reviewer may limit that review
to the files the sync changed. The gate (the set of checks that `AGENTS.md`
defines) runs in every case.

The orchestrator merges into `main` only when all four are true:

- The PR is open on upstream, `OWNER/REPO`, with base `main`.
- A reviewer approves the current head SHA.
- The PR branch contains the current `main`. The ancestor check asks git
  whether one branch contains another. The check is in the `delegation-loop`
  skill.
- The repo's full gate, as its `AGENTS.md` defines it, passes.

The reviewer is a different agent from the worker. The gate command is in the
repo's `AGENTS.md`. If the repo has no gate command, report that. Do not merge.

A branch with no open upstream PR is not merged.

When all four conditions above are true, merge the PR with this command. No
owner sign-off is needed. An approval at the current head SHA is still required.
Record the approved SHA before you merge. The command creates a merge commit:

```sh
gh pr merge <PR-number> --repo OWNER/REPO --merge \
  --match-head-commit <approved-SHA>
```

Do not rebase. Do not force-push. A worker or reviewer never merges a pull
request.

### Continuous merge of approved PRs

On every orchestrator cycle, merge each approved PR into `main`. Do this
until no approved PR is left.

1. List the open PRs that target `main`:

   ```sh
   gh pr list --repo OWNER/REPO --base main --state open \
     --json number,headRefOid,headRefName,headRepositoryOwner,isDraft
   ```

   `headRepositoryOwner.login` names the owner of the repository that holds the
   branch. Use it to choose `origin` or `fork` for `REMOTE` in the ancestor
   check in "Review and merge each pull request".

2. For each PR, get the commit of its latest approval:

   ```sh
   gh api repos/OWNER/REPO/pulls/<PR-number>/reviews \
     --jq '[.[] | select(.state == "APPROVED")] | last | .commit_id'
   ```

   The output is `null` when no reviewer approved the PR.

3. Merge the PR only when that commit equals its `headRefOid`. A push after the
   approval changes the head, so the PR waits for a new approval. Then run the
   four merge gates above. If all four pass, merge with the command in "Review
   and merge each pull request".
4. Merge one PR at a time. List the PRs again after each merge, because
   `main` moved. If a merge fails or the head changed, stop for that PR
   and report it. Do not retry in the same cycle.

The owner can also merge an approved PR. List the PRs again after any merge.

### Preview server

Each repo runs one preview server. It serves upstream `main` only. Do
not run a server for a feature branch.

1. Create the preview worktree once. Detached means the worktree follows a
   commit, not a branch. From the repo root, run:

   ```sh
   git fetch origin
   git worktree add --detach .claude/worktrees/preview origin/main
   ```

2. After each merge into `main`, the orchestrator updates the preview.
   From the repo root, run:

   ```sh
   git -C .claude/worktrees/preview fetch origin
   git -C .claude/worktrees/preview checkout --detach origin/main
   ```

   Then it restarts the server.
3. To bind means to choose the address a server listens on. Bind the server to
   `127.0.0.1` or to the Tailscale IP address. Tailscale is a private network
   that links your own machines. Never bind to `0.0.0.0`.
4. Set `LUPIN_LOOP_STATE_DIR` to a scratch path. Do not point the preview at
   the real fleet Redis unless the test needs it.
5. Keep the server running in a Herdr pane or with `systemd-run --user`. The
   `systemd-run` command starts a command as a background service. The server
   must keep running after an agent session ends.
6. The repo's `AGENTS.md` has a "Preview server" section. It names the start
   command, the port, and the machine. It gives the tunnel command,
   `ssh -N -L PORT:127.0.0.1:PORT MACHINE`, and the local URL,
   `http://localhost:PORT`. The port must be free on that machine. Check with
   `ss -ltn`. The `ss` command lists listening ports. If the repo has no
   runnable app, that section says `Preview server: none` and gives the reason.
7. A PR does not need a tunnel command. The PR body says whether the change is
   visible only after the merge.

## Monitor and finish

Use `lupin machines`, `lupin peek`, and `lupin attach` to check live work. On
a Herdr worker, `herdr --session lupin-loops agent list` shows each loop's
state, workspace, and pane in one call. Read the final diff and run the
repo's required gate and a smoke check. A worker's success report is not
proof. Re-read issue and PR comments before closure.

After the merge and the preview restart, check that the preview port answers.
Run `curl -fsS -o /dev/null http://127.0.0.1:PORT/` on the preview machine. Use
the Tailscale IP address instead if the server binds to one. Exit code 0
means the port answers. Report the result in the handoff.

Append dispatch and handoff events to the shared Redis ledger:

```sh
lupin ledger append OWNER/REPO --event dispatch --issue N \
  --status running --branch BRANCH
lupin ledger append OWNER/REPO --event handoff --issue N --status STATUS \
  --summary TEXT --highlights TEXT --evidence TEXT --decisions TEXT --next TEXT
lupin ledger read OWNER/REPO --json
```

Add `--child N` for each split issue. If Redis is unavailable, ledger
commands exit 3. The roadmap shows no ledger annotations and adds a warning.
Use `.loop/loop-state.json` only when a ledger command exits 3. The
`handoff` skill gives the format. At the start of a session, read the ledger
and that file.

A worker's task ends when its issue is merged into `main`. A task also
ends when it is blocked on an owner decision. A pull request that waits for
review is not blocked. The orchestrator dispatches the review and any fix. The
fix goes on the same branch.

"Blocked" means an owner decision is needed. An example is a model-routing
policy change.

Policy changes to this skill go to upstream `main` as a pull request, which the
owner merges.

The loop is not finished while an unblocked PR waits for a review, a fix, or a
merge.

Continue working until the backlog is empty or every remaining item is blocked
on an owner decision, and every open PR is approved at its current head or has
a live worker. Then run `/handoff`. State the exact next action and any
missing owner input.
