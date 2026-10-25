# lupin agent and repository rules

This file contains agent instructions and repository rules. See the
[user guide](docs/user-guide.md) for command and state behavior.

## Host-specific setup

Each run gets its repo's GitHub token as `GH_TOKEN`. The host maps repo names
to token files with `LUPIN_LOOP_CREDENTIALS_FILE`; see `docs/configuration.md`.

The coordinator reads one signing-key file per worker from
`LUPIN_CMD_SIGNING_KEYS_DIR`, or from systemd's `CREDENTIALS_DIRECTORY` if the
first setting is unset. Each worker runs `lupin agent` with its key. The sender
also needs the target worker's key. Inject keys only into the Lupin process,
through protected secret management. Do not put them in command arguments or
global environment settings.

The dispatch host runs `fleet-run` on a systemd timer. The default schedule
runs at 00:15, 05:15, 10:15, 15:15, and 20:15 each day. The host can change
the schedule. The dashboard can start or stop the timer. The dispatch host
has no Herdr and runs no loops. Use the dashboard for all machines, and Herdr
on a worker for its loops.

## Dashboard mockups

`docs/ui.dc.html` has the dashboard mockups. Read the matching section before
you change a dashboard page. For example, the Machines page is section `1k`.
`docs/ui-mockups.md` has a screenshot for each section. Use `docs/ui.dc.html`
for layout and content. Treat its values as sample data. Keep real data and
controls accurate. Do not show shared fleet capacity as a per-machine limit. Do
not add controls that the current code cannot support.

## Code layout

- `src/lupin/route.py`, `src/lupin/classify.py`, `src/lupin/model-tiers.json`:
  from an earlier project. Comments at the top give the source commit.
- `src/lupin/slots.py`: the `local` slot backend.
- `src/lupin/slots_redis.py`: `redis` slot backend; `bmo` falls back to `local`.
- `src/lupin/_lease_runtime.py`: `hold` and lease renewal, for both backends.
- `src/lupin/review_dispatch.py`: picks which lock a routed model needs.
- `src/lupin/pr_status.py`: `pr-status`. One read-only view of every open PR,
  from one `gh pr list` call. It shows merge, checks, review, approval, claim,
  comments, description, and reviewers.
- `src/lupin/free_gate.py`: routes, then reserves the fleet `bmo` Redis slot for
  a `bmo:` pick before the pick is final.
- `src/lupin/serve.py`, `src/lupin/roadmap.py`: the dashboard.
- `src/lupin/quota.py`: the `/usage` page's quota reads.
- `src/lupin/quota_cache.py`: `lupin quota`'s shared Redis cache.
- `src/lupin/usage_cache.py`: each machine's 7-day usage totals.
- `src/lupin/model_fetch.py`: `fetch-models`.
- `src/lupin/model_tiers.py`: loads and validates the model-tier file.
- `src/lupin/claims.py`: `claim`, `renew-claim`, `release-claim`.
- `src/lupin/dispatch.py`: `dispatch`.
- `src/lupin/digest.py`: writes a digest for each debrief.
- `src/lupin/ledger.py`: appends and reads shared repo events.
- `src/lupin/machines.py`: `join`, `heartbeat`, `drain`, `undrain`, `machines`.
- `src/lupin/loops.py`: signed local or remote dispatch for loop controls.
  Attach uses Herdr directly over SSH.
- `src/lupin/loop_runtime.py`: Herdr sessions, workspaces, state, reports,
  schedules, and systemd worker lifecycle.
- `src/lupin/run_log.py`: one event log per loop run.
- `src/lupin/quest.py`: `quest start`, `stop`, `focus`, `release`.
- `src/lupin/place.py`: `place`. Picks the machine for a task.
- `src/lupin/supervise.py`: `supervise`.
- `src/lupin/reconcile.py`: `reconcile`. Automatic claim, focus, quest release.
- `src/lupin/roadmap_cli.py`: the `roadmap` CLI. `roadmap.py` is separate.
- `src/lupin/cli.py`: the `lupin` command. Parses arguments for all subcommands.
- `tests/`: one test file per module above.

## Build and test

Full gate:

```
nix flake check                                    # build + test, current system
nix develop -c python3 -m pytest -v
```

`nix develop <dir> -c <command>` runs `<command>` in the current directory, not
in `<dir>`. A relative test path then picks up files from the directory you are
in. Run `cd <checkout>` first. To test a reviewed commit, run `git archive <sha>
| tar -x -C <empty-dir>`. Then run the tests from `<empty-dir>`.

## Commands in docs and skills

Commands in docs and skills must work for any user of this tool. Use
`OWNER/REPO` for the repo, `origin` for the upstream remote, and `<checkout>`
for a path. Do not put a real owner name or machine path in a command. Prose may
name this repo.

## Pull requests, review, and handoff

Every code change goes through a pull request (PR) to upstream `main`. Upstream
is the `origin` remote. The owner merges every PR on `origin`. A loop never
merges on `origin`. The loop policy is in `docs/delegation-loop.md`.

1. Push the branch to your fork with `git push -u fork <branch>`. Never push to
   `origin` or `main`. Do not create a new fork. You may use `/ship`. Check that
   it does not push to `origin` or `main`. Open the PR on upstream, with the
   fork branch as head:

   <!-- markdownlint-disable MD013 -->
   ```sh
   gh pr create --repo OWNER/REPO --base main --head FORK_OWNER:<branch> --title "<title>" --body "Closes #N"
   ```
   <!-- markdownlint-enable MD013 -->

   The PR body has one line per issue. Use `Closes #N` when the PR finishes the
   issue. Use `Refs #N` only when work stays open. If the push fails, report the
   branch name and commit range. Do not review or merge that branch. If the PR
   opens, report its number to the orchestrator, which dispatches
   `/code-review`.

   Stage every file you change before you commit it. If you edit a file after
   `git add`, run `git add FILE` again. After the commit, run `git diff --stat
   HEAD`. It must print nothing.
2. A reviewer who is not the author runs `/code-review`. A PR is ready for
   production when its latest formal review approves the current head SHA
   (`commit_id`). The repo's full gate, as its `AGENTS.md` defines it, must also
   pass at that SHA. A push after an approval needs a new review. Merge
   `origin/main` into the branch only when GitHub reports `CONFLICTING`. Resolve
   the conflicts in that merge. Do not rebase. The merge is a push, so it needs
   a new review. A worker or reviewer never merges a PR.
3. At the end of a session, run `/handoff`. It runs `lupin ledger append`.

### Preview server

This repo has a runnable dashboard. Its preview serves `main`.

- Worktree: `.claude/worktrees/preview`. The skill gives the setup commands.
- Start command. Run it from the preview worktree:

```sh
nix develop --command env \
  -u LUPIN_REDIS_USERNAME -u LUPIN_REDIS_PASSWORD \
  LUPIN_REDIS_HOST=127.0.0.1 LUPIN_REDIS_PORT=6399 \
  LUPIN_FLEET_CONFIG="$HOME/.local/state/lupin-preview/fleet.json" \
  LUPIN_LOOP_STATE_DIR="$HOME/.local/state/lupin-preview" PYTHONPATH=src \
  python3 -c '
import sys
from lupin.cli import main
sys.exit(main(["serve", "--bind", "127.0.0.1", "--port", "8789"]))
'
```

- Port: `8789`. Check that it is free first: `ss -ltn` lists listening ports.
- Machine: the owner names it in the owner's machine notes. The preview serves
  `main` there on `127.0.0.1:8789`.
- Owner tunnel command: `ssh -N -L 8789:127.0.0.1:8789 MACHINE`. A tunnel
  forwards a port on your computer to the machine.
- Local URL: `http://localhost:8789`.
- Keep it running in a Herdr pane, or with `systemd-run --user`. If the shell
  has no user bus, set `XDG_RUNTIME_DIR=/run/user/$(id -u)`.
- Never bind to `0.0.0.0`. To bind is to choose the address a server listens on.
- Do not point the preview at the fleet Redis. Start a scratch Redis, for
  example `nix develop --command redis-server --port 6399 --bind 127.0.0.1
  --save ""`. Check that port `6399` is free with `ss -ltn` before you start it.
  Redis is not on the PATH outside the dev shell, so run it through `nix
  develop`. The start command replaces inherited `LUPIN_REDIS_*` values with
  scratch ones.
- Do not use the start, stop, or run controls on this dashboard. New runs use
  the `lupin-loops` session. Older loops may use an old session until they stop.

## Rules for unattended loops

1. **Do not use the question menu.** The menu waits for a person to choose. No
   person is there. Write the question as a comment on the issue. Start it with
   `Loop:`. Then take the next item.
2. **Do not stop while work exists.** An unmerged PR is not a reason to stop.
   Reviews, research, and docs are work. Do them.
3. **Use the fork login for fork pushes.** On a host where `GH_TOKEN` cannot
   push to the fork, the saved fork login can. `FORK_OWNER` is the fork owner on
   that host. Put `env -u GH_TOKEN` before each command. It removes `GH_TOKEN`
   for that one command only. Do not run `unset GH_TOKEN`:

   <!-- markdownlint-disable MD013 -->
   ```sh
   env -u GH_TOKEN git push -u fork <branch>
   env -u GH_TOKEN gh pr create --repo OWNER/REPO --base main --head FORK_OWNER:<branch> --title "<title>" --body-file <file>
   ```
   <!-- markdownlint-enable MD013 -->

4. **Do not run `rm` with a glob.** A glob is a pattern such as `*`. Give each
   new file a new name. Do not delete old files with a pattern.
5. **Ask a worker with no result for it.** If a worker has no result 30 minutes
   after it starts, ask it for the result. If it has none, write that on the
   issue or the PR. Start it with `Loop:`. Then stop the worker. The
   `delegation-loop` skill shows how to ask a worker (`herdr agent prompt`).
6. **File environment problems in the issue tracker.** The tracker is
   `gracecraft/ghostbook.nix`. The old name, `gracecraft/nix`, redirects there.
   On a host where `GH_TOKEN` cannot read the tracker, use the `gracecraft`
   account for that command. Pass the token inline:

   ```sh
   GH_TOKEN="$(gh auth token --user oazise)" gh issue create \
     --repo gracecraft/ghostbook.nix \
     --label environment --title "<machine>: <problem>" --body-file -
   ```

   If `gh` cannot write, log the problem in the ledger:

   ```sh
   lupin ledger append gracecraft/ghostbook.nix --event env-friction \
     --summary "<problem>"
   ```

7. **Do not stop after a review.** A review with findings starts the next round.
   Send the findings to a fix worker. Then get a new review at the new head.
   Repeat until the PR is ready for production (see "Pull requests, review, and
   handoff"). There is no limit on rounds. If the same finding comes back for 3
   rounds, give the fix worker a model one tier higher.
8. **Work the queue until nothing is left.** Nothing is left when all of these
   are true. No open issue that you may take is unclaimed. Every PR you own is
   ready for production. No PR waits for a review. No research, docs, or triage
   item is left. Waiting for the owner is not the end. Ask as rule 1 says, then
   take the next item. Never write "tell me if you want me to continue".
