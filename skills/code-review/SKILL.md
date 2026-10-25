---
name: code-review
description: >-
  Review GitHub pull requests for correctness, safety, and maintainability.
  Use for every change before the owner merges it.
compatibility: Requires GitHub CLI, a pull request, and access to the repository.
---

# Review a pull request

Review the pull request (PR), not a local diff. Pass `--repo OWNER/REPO` to every
command.

The orchestrator holds the Lupin claim for your review. Do not claim the PR. If
your brief names no claim, stop and ask the orchestrator. If there is no PR, ask
the orchestrator to open one.

## Read

```sh
lupin review-route --prefetch 123 --repo OWNER/REPO
gh pr view 123 --repo OWNER/REPO --json title,body,comments,reviews
gh pr diff 123 --repo OWNER/REPO
gh pr checks 123 --repo OWNER/REPO
```

Also read the repo's `AGENTS.md`. Review the current head again each time the PR
changes. Do not edit files or merge the PR.

## Check

Check the diff against the issue's acceptance checks, the repo rules, and these
lenses. Passing CI does not prove the design is sound.

| Lens | Look for | Label |
| --- | --- | --- |
| `bug` | Wrong result, crash, or missed edge case (empty input, zero, last item, time zones). Grep every caller of a changed function. | `🔴 bug` |
| `risk` | Security hole, unsafe access, data loss, an ignored error, or writes that must all succeed together but do not. | `🔴 bug` |
| `scale` | Fine for one user, wrong for many. Read the expected load in the README and deploy config first. | `🟡 risk` |
| `missing test` | Risky new logic has no test that fails when the logic breaks. One good test is enough. | `🟡 risk` |
| `speed` | A big slowdown. | `🟡 risk` (big), `🔵 nit` (small) |
| `lean` | Dead code, a helper the repo already has (name its path), a dependency for a few lines, an abstraction with one use, near-copies, or tests that check nothing. | `🟡 risk` if it adds code, `🔵 nit` for a shorter form |

Also:

- Find docs that the change makes wrong.
- A visual change with no before and after screenshots on the PR is a `🔴 bug`.
  A new screen needs no before. For 3D changes, inspect the contact sheet.
- An owner comment that names a problem is a finding. A PR comment is never an
  approval.
- A `shortcut:` comment that names its limit is a decision. Report it only if
  the expected load passes that limit.

Before you report a finding, name the input that fails. No input, no finding.
Re-read the lines and grep for callers. Propose the smallest fix, and prefer
fixes that delete code. Give no style preferences and no vague advice.

## Output format

Use short sentences and everyday words. Explain each technical term once.

1. First line: `Round N. LEVEL: <LEVEL>. Target: OWNER/REPO PR #<number> at <full head SHA>.`
2. Second line: `What this change does:` and two or three sentences.
3. Findings, numbered across all groups so the author can say "fix 2 and 5".
   Skip an empty group.
   - **Must fix:** each `🔴 bug`, and each `🟡 risk` that breaks at the expected
     load.
   - **Should fix:** other `🟡 risk`.
   - **Nice to have:** `🔵 nit`, and `❓ q` (a question; it blocks only if the
     answer changes what to do).
4. `Verdict: Ship.` or `Verdict: fix 1 and 3 first.` Use `Ship` only when no
   blocking finding remains.
5. Optional: `Lean: -<N> lines possible.` and `Not checked: <one line>`.

With no findings, write lines 1 and 2, then `Looks good. Ship.` and one line on
what you checked.

One finding looks like this:

```text
2. **Empty token passes the check** (`src/auth.py:L42`, `🔴 bug`)
   - **Problem:** The check accepts an empty token, so a request with no token passes.
   - **Fix:** Reject an empty token before the check runs.
   - **If we skip it:** Anyone who sends an empty token can use the endpoint.
```

## Run the fix cycle

Post each verdict as a GitHub review bound to the exact SHA you read. Use
`APPROVE` for `Ship` and `REQUEST_CHANGES` for findings:

```sh
gh api repos/OWNER/REPO/pulls/<number>/reviews \
  -f event=APPROVE -f commit_id=<full SHA> -F body=@verdict.md
```

Do not use `gh pr review`; it may bind the review to a newer head.

Use `-f event=REQUEST_CHANGES` when the review asks for changes. If your login
opened the PR, post `COMMENT` and ask the owner for a formal review in a `Loop:`
PR comment.

- N is 1 more than the N in the latest `Round` review on the PR (1 if none).
- LEVEL is the next fix worker's LEVEL. Take it, and the stall rule, from "Fix
  worker models" in the `delegation-loop` skill. Check the stall rule before you
  post.
- If the latest `Round` review is already at the current head SHA, do not review
  again. Start the fix worker with its findings.

Run the cycle only when your brief says the loop owns the PR. Otherwise post the
verdict, report to the orchestrator, and stop.

1. After a blocking verdict, start a fix worker (your runner's agent tool, or
   `herdr agent start` in a new pane). Stop and report instead if the stall rule
   blocks the PR, if LEVEL is `size-l` and you are `size-m`, or if you cannot
   start an agent.
2. Give it a brief with the PR, branch, remote, findings, and gate output. Tell
   it to follow `/ship` for the fix, gate, and screenshots. Use the PR-branch
   worktree steps in the `delegation-loop` skill; it pushes to the same branch.
3. Every 60 seconds, read the head SHA:
   `gh pr view <number> --repo OWNER/REPO --json headRefOid -q .headRefOid`.
   Stop waiting when it changes or the fix worker ends. If it ends with no push,
   follow steps 4 and 5 of "Fix worker models".
4. When the SHA changes, review it as the next round. Repeat until
   `Verdict: Ship`.
5. If the stall rule blocks the PR, post the `Loop:` comment from step 3 of "Fix
   worker models" and report to the orchestrator.

Report the final verdict to the orchestrator. The owner merges. You never merge.
