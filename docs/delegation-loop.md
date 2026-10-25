# lupin — the delegation loop

This file is optional. Read the root `AGENTS.md` first.

## Where this repo lives

This repo is `OWNER/REPO`. The owner mounts a local checkout at
`/code/<checkout>`. A loop agent does not edit this main checkout. Each run gets
its own Git worktree in the loop state directory. See "State" in `AGENTS.md`.

## Lupin commands for a pass

| Job | Command |
| --- | --- |
| Read the latest events | `lupin ledger read OWNER/REPO --json` |
| Rank open issues (`--repo` is the checkout folder name under `LUPIN_LOOP_CODE_DIR`, default `/code`) | `lupin roadmap --repo lupin --stage all --json` |
| Show dependencies | `lupin roadmap --repo lupin --dag` |
| Select a model and a machine | `lupin place N --explain` |
| Show provider quota | `lupin quota` |
| Show every open PR in one view | `lupin pr-status` |
| Claim an issue | `lupin claim OWNER/REPO#N --holder SESSION` |
| Brief and start one issue or PR (unless no-start is passed) | `lupin dispatch OWNER/REPO#N` |
| Claim related issues together | `lupin quest start --issue N --issue M` |
| See machines and loops | `lupin machines`, `lupin loops`, `lupin peek lupin` |
| Write a handoff at the end of a session | `/handoff`. It runs `lupin ledger append`. |

## Rule 1: push the branch, open the PR upstream

A worker pushes its branch to the fork `FORK_OWNER/REPO`. Do not test access
with `gh api repos/OWNER/REPO --jq .permissions.push`. Try the push. Use the
existing fork. Do not create a new one. Do not open a PR or an issue on a fork.

1. Fetch upstream. From the repo checkout, run `git fetch origin`.
2. Create a worktree under `.claude/worktrees/`. Run `git worktree add <path> -b
   <branch> origin/main`.
3. Add the fork remote if it is missing, then push the branch to the fork. Never
   push to `origin` or `main`. Use `/ship` for steps 3 and 4.

   ```bash
   git remote add fork https://github.com/FORK_OWNER/REPO.git
   git push -u fork <branch>
   ```

4. Open the PR on upstream, with the fork branch as head. Use `Closes #N` when
   the PR finishes the issue. Use `Refs #N` only when it leaves work open.
   Report the PR number to the orchestrator (the agent that dispatches work).
   The orchestrator dispatches `/code-review`.

   <!-- markdownlint-disable MD013 -->
   ```sh
   gh pr create --repo OWNER/REPO --base main --head FORK_OWNER:<branch> --title "<title>" --body "Closes #N"
   ```
   <!-- markdownlint-enable MD013 -->

On a Herdr worker that opens a new branch, use these in place of step 2:

```bash
herdr worktree list --cwd <checkout>
git -C <checkout> fetch origin
herdr worktree create --branch <branch> --base origin/main --cwd <checkout>
```

If the checkout has neither an `origin` nor a `fork` remote, stop. Report the
missing remote. Add `fork` only as step 3 says.

`lupin run` starts from `origin/HEAD`, or else `origin/main`.

## Pull request and review

Every feature change goes through a pull request. A change to this policy also
goes to upstream `main`. In this repo:

1. A worker follows rule 1. If the fork push fails, the worker stops. Report the
   branch name and commit range. Do not review the branch.
2. The orchestrator dispatches `/code-review` for every PR, even a docs-only
   one. The reviewer is not the worker and has an equal or higher model tier.
3. If the review finds a problem and the loop owns the PR, the reviewer starts
   the fix worker and runs the fix cycle until the PR is ready for production.
   See "Run the fix cycle" in the `code-review` skill.
4. The reviewer posts each verdict on the PR.
5. The owner merges. A PR is ready for production when its latest formal review
   is an approval at the current head SHA, and the repo's full gate passes at
   that SHA. A loop never merges on `origin`. A worker or reviewer never merges
   a PR. Do not force-push.
6. A conflict with `main` needs a sync. GitHub reports it as `CONFLICTING` in
   `mergeable`. Merge `origin/main` into the PR branch and resolve the conflicts
   in that merge. Do not rebase. A sync is a push, so it needs a new review.
7. PRs come first. In each cycle, dispatch PR work before new issues. A PR in a
   fix cycle does not delay new work.

### Preview server

This repo follows "Preview server" in the `delegation-loop` skill, plus:

- The start command and port are in "Preview server" in `AGENTS.md`. The machine
  is in the owner's own machine notes.
- Set `LUPIN_LOOP_STATE_DIR` to a preview-only path. The default is
  `/var/lib/delegation-loop`, the real fleet state.
- Do not point the preview at the fleet Redis. Use a scratch Redis.
- New runs use the `lupin-loops` session (`SESSION_NAME` in
  `src/lupin/loop_runtime.py`). The state directory does not change it. Older
  loops may use an old session until they stop.
- Do not use the start, stop, or run controls on the preview dashboard.

## Other rules

Rule 1 above applies. Before you dispatch anything, check for work already done:

```bash
git -C <checkout> fetch origin
git -C <checkout> branch --no-merged origin/main
```

## Blocked tasks

When a task seems blocked, delegate a research worker first. Act on its result.
Wait for the owner only for credentials, money, hardware, a product choice, or
an action the repo rules reserve for a person. Ask with an issue comment that
starts with `Loop:` and lists the options.

For another block, retry a transient error three times, five minutes apart.
Then use the documented fallback. If no competent model is affordable, research
lower-cost options before you defer the task. Record the reason and take the
next item. Do not wait with no next action.
