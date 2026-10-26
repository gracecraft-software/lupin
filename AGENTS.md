# lupin

`lupin` runs task loops on many machines. It has four jobs.

1. Routing. It picks a model and an effort level for a task. Commands: `route`,
   `classify`, `review-route`. `fetch-models` saves a daily snapshot of the
   models each subscription can call, with prices.
2. Slots. A slot limits how many callers use a resource at one time. Commands:
   `acquire`, `hold`, `release`, `status`.
3. Dashboard. It shows the loops and controls them. Command: `serve`.
4. Fleet. It shares issue claims, machine records, quests (issues worked
   together on one machine), scheduled runs, and repo events. Commands: `claim`,
   `renew-claim`, `release-claim`, `join`, `heartbeat`, `drain`, `undrain`,
   `machines`, `place`, `quest`, `reconcile`, `roadmap`, `ledger`, `fleet-run`.
   Loop commands: `run`, `once`, `enable`, `disable`, `loops`, `stop`, `peek`,
   `logs`, `attach`, `schedule`, `pause`, `resume`, `supervise`.

Run `lupin --help` for the full list.

## Commands

```
lupin route <category> <size> [--no-bmo] [--primary-effort E] [--json]
lupin classify --issue-json FILE [--diff-stat FILE] [--json]
lupin fetch-models [--snapshot-file FILE] [--no-write] [--json]
lupin fetch-benchmarks [--force] [--json]
lupin quota [--json]
lupin review-route (--category C --size S | --issue-json FILE) [--mode M]
lupin review-route --prefetch N[,N...] [--repo OWNER/REPO] [--json]
lupin pr-status [--repo OWNER/REPO] [--json] [--full]
lupin acquire <slot> --holder H [--wait SECONDS] [--max N] [--ttl SECONDS]
lupin hold (--lease ID | <slot> --holder H --wait S) [--ttl SECONDS] -- <command>
lupin release --lease ID
lupin status [--json]
lupin dispatch OWNER/REPO#N [--holder HOLDER] [--json] [--no-start]
lupin ledger append OWNER/REPO --event EVENT [--issue N] [--status S] \
  [--branch B] [--summary TEXT] [--highlights TEXT] [--evidence TEXT] \
  [--decisions TEXT] [--next TEXT] [--child N] [--json]
lupin ledger read OWNER/REPO [--limit N] [--json]
lupin serve [--bind 127.0.0.1] [--port 8788] [--roadmap REPO]
lupin agent [--machine M] [--batch N] [--poll-interval S]
lupin run <repo> [--machine M] [--platform claude|omp] [--provider P] [--model M] [--note TEXT] [--resume]
lupin fleet-run [--json]
lupin supervise [--every [SECONDS]] [--dry-run] [--json]
lupin once <when> [repo ...] [--platform claude|omp] [--provider P] [--model M] [--note TEXT] [--resume]
lupin enable <repo> [--platform claude|omp] [--orchestrator SELECTOR ...] [--clear-orchestrators]
lupin disable <repo>
lupin loops [repo] [--machine M] [--json]
lupin stop <repo> [--machine M] [--force] [--wait S] [--json]
lupin peek <repo> [LINES] [--machine M] [--json]
lupin logs <repo> [--run ID] [--tail N] [--json]
lupin attach <repo> [--machine M] [--print]
lupin schedule [--machine M] [--json]
lupin schedule cal <expr> [--machine M] [--json]
lupin schedule first <when> every <interval> [--machine M] [--json]
lupin pause [--machine M | --all] [--json]
lupin resume [--machine M | --all] [--json]
```

`route` and `classify` print a plain result by default (`model effort`, or
`category size`). A blocked route tries the other models in its tier, then
lower tiers. It does not use a higher tier to avoid a quota wait. If quota
blocks every allowed model, `route` prints a deferral reason and wait time
instead of a model. Add `--json` for a JSON object; a deferred route includes
`reason` and `wait_seconds`. If the category or size is not known, `route`
and `review-route` print one line to stderr. The line lists valid values.
The exit code is 2.

`ledger append` stores an event in Redis, with the time and host. Repeat
`--child` for each split issue. Repeat a digest option to add more items.
`ledger read --json` returns the latest 10 events, oldest first. `--limit N`
sets another positive count. `read_events` decodes each entry alone with
`_decode` in `src/lupin/ledger.py`. It skips an entry when a field is not valid
UTF-8, or when `_decode` raises KeyError, ValueError, or RecursionError. It
reports each skipped entry on stderr. A skipped entry still counts toward the
limit. `_decode` raises ValueError when a list field nests brackets more than 8
levels deep. A real field has 1 level. Both commands exit 3 when Redis is
unavailable. Then the roadmap shows no ledger annotations and a warning. The
roadmap ignores `.loop/loop-state.json`.

`review-route --prefetch` fetches the body and comments of an issue or PR. For a
PR, it also fetches the reviews and a diff stat (files, additions, deletions; no
diff text). It prints one `brief_markdown` section per item. `--json` keeps only
the fields a worker reads. Comments keep `author.login`, `createdAt`, and
`body`. Reviews keep `author.login`, `state`, `submittedAt`, and `body`. The
item keeps `kind`, `body`, and `diff_stat`. A number that is neither an issue
nor a PR gets `{"error": ...}` in its own slot.

`dispatch OWNER/REPO#N` claims the item. It writes a markdown brief under the
loop state directory's `notes/`. The brief has the body and every comment. For a
PR, it also has the reviews and changed files. Then `dispatch` starts the loop
with the brief. `--no-start` writes the brief only, and the claim stays with the
holder. `--json` prints the brief path and the claim. A brief over 8,000
characters becomes a short note that names its path. The brief is never
committed.

`acquire` prints a lease ID. Exit code 2 means the slot is full. The caller
skips and tries later. Exit code 3 means the `redis` backend cannot reach the
coordinator. Only the `bmo` slot falls back to `local`. When Redis is
unreachable, `acquire`, `renew`, `release`, and `status` use `local` for `bmo`,
and print a warning.

A refused login raises `CoordinatorAuthFailed`. An ACL denial raises
`NoPermissionError`. Both apply to every slot, and neither falls back. `lupin`
then exits with code 3 and prints one line that names the setting to check. For
a refused login, that setting is `--redis-password` or `LUPIN_REDIS_PASSWORD`.
For a fleet command, the line also names the systemd credential
`redis-password`. The line never shows a password. Some read paths are not
covered yet. See `docs/redis-schema.md`. The `local` backend never exits with 3.

`hold` gets a lease, or reuses one from `--lease`. It runs `<command>` and
renews the lease while the command runs. It releases the lease when the command
ends, by normal exit, non-zero exit, or signal.

`fetch-models` checks which model IDs each subscription can call, and what they
cost. It saves the result to `~/.local/state/lupin/model-snapshot.json`, unless
`--no-write` is set. It also publishes the snapshot to Redis. A Redis error
keeps the local file and prints a warning. For opencode-go, it finds the API key
in the order that `quota` uses (see below). An expired key makes the row say
"OpenCode Go API key expired". For Codex, it uses `omp models` when available.
Otherwise it uses models.dev and marks the rows `live: false`. Prices come from
models.dev, matched by model ID. A model with no match has `price: null`.
`promo` stays `null`. A failed fetch, for example from an expired OAuth token or
a missing key, keeps the last verified list. It marks the list `live: false`
with the reason. The Models page shows that reason under each row.

`run <repo>` starts a Lupin worker and a Herdr workspace. The agent works in a
new Git worktree (see "State"). `run --all` starts every enabled repo.
`--machine M` uses the signed fleet queue on another machine. `run --all
--machine M` starts that machine's enabled repos.

Each run has one claim holder: `<host>:<repo>:<run branch>`. The agent's shell
gets it as `LUPIN_CLAIM_HOLDER`. `claim`, `renew-claim`, and `release-claim` use
it when `--holder` is not given. The monitor renews each claim of that holder
every 2 minutes. It stops when the workspace closes or the Herdr server stops.
It also renews while the agent waits for input (state `done`). It renews no
claim of another holder. When renewals stop, the claims expire within 10
minutes. A finished loop with an open workspace keeps its claims until `lupin
stop`. A renew error does not stop the loop. The monitor log gets one line per
new error.

Without a profile, Lupin uses the repo's configured platform. To let Lupin
choose among orchestrators, add a profile when you enable the repo. Repeat
`--orchestrator` for each choice:

```
lupin enable my-repo --platform omp \
  --orchestrator openai/gpt-5 \
  --orchestrator opencode-go/deepseek-v4.1-flash:xhigh \
  --orchestrator claude
```

Profiles are local to each worker. Set them on each worker that can start the
repo. At loop start, Lupin uses quota data from the shared cache. The data
must be less than five minutes old and include usable quota for a candidate.
Claude's weekly quota is blocked above 90%. This limit has no reset grace.
Lupin skips blocked providers, then picks the candidate with the most quota
headroom across its known windows. For a provider with more than one login,
headroom is that of the best login that is not blocked. Profile order breaks
ties. If no candidate
has fresh, usable quota, Lupin refuses to start the loop.

While a loop runs, its monitor checks the active provider's shared quota every
30 seconds and when the agent returns to the monitor. At 10% or less quota
remaining, Lupin asks the agent to stop new work and save progress, active work,
blockers, and one next action in `HANDOFF.md`. It keeps the workspace and claim.
If quota data is not fresh, Lupin stops the agent without a provider request and
writes a handoff with the reason.

If usage ends first, Lupin records an interrupted state and reason. With a
fresh, usable alternate provider in the repo's profile, Lupin starts it in the
same workspace and worktree with the handoff file. Otherwise, the loop needs
attention, and the monitor keeps renewing its claim while it waits.
`lupin loops REPO` shows the reason and handoff path. The monitor resumes when
a configured alternate provider has fresh, usable quota.

Explicit `--platform`, `--provider`, or `--model` selects one fixed orchestrator
for the run, and skips profile routing. For OMP, use `--platform omp`, and
optionally `--provider openai-codex|opencode-go` or `--model MODEL`. `openai`
and `openai/MODEL` still work as aliases for `openai-codex` and
`openai-codex/MODEL`. Examples: `--model openai-codex/gpt-5.6-luna`, `--model
opencode-go/deepseek-v4.1-flash:xhigh`. Set auto-compaction for the selected
model in OMP, near 200k tokens. Lupin passes the model to OMP. It does not set
or check OMP's compaction. These options work with `run` and `once`, and future
one-off runs keep them. `run --all` applies them to each repo.

`once now` starts the listed repos, or every enabled repo when none is listed. A
future `once` run uses the same default. It fails if no repo is enabled.
`schedule first` sets the first timer run. Use `+2h` for a relative time, or a
calendar expression such as `tomorrow 09:00`. The interval sets later runs.
`disable` also removes the repo's orchestrator profile. To clear a profile
without disabling the repo, run `enable --clear-orchestrators`.

`fleet-run` sends each enabled repo to one online worker. It skips the local
machine, workers with an active loop for that repo, and workers without the
repo's checkout. Run-now actions from `fleet-run` and the dashboard use the
target worker's local platform and profile. Configure the profile on each worker
that can start the repo.

The coordinator needs one signing key file per worker. Lupin reads them from
`LUPIN_CMD_SIGNING_KEYS_DIR`, or from systemd's `CREDENTIALS_DIRECTORY` when the
first is unset. Each file name is the worker name. Each worker runs `lupin
agent` with its key. The command queues runs. It does not wait for them to
start.

`supervise` checks this machine's loops with fixed rules. It uses no model. For
each loop, it reads the Herdr state of the pane and the Lupin loop state. It
also reads the systemd unit, the last 20 pane lines, and the newest handoff from
this run. If Herdr does not list the loop's own pane, the Herdr state is
`unknown`. The first rule that matches gives the verdict:

1. `stopped`, reason "waiting for the owner". Herdr says `blocked`, or the Lupin
   state is `needs_attention`. The loop is flagged. No nudge.
2. `stopped`. Work is left, and the unit is `inactive` or `failed`. The loop is
   flagged. A nudge is sent only when Herdr says `idle` or `done`.
3. `busywork`. Herdr says `working`, and the pane text matches the last pass.
   The loop is flagged. No nudge.
4. `idle`. Work is left, and Herdr says `idle` or `done`. The nudge is sent. The
   pane or the handoff can contain "tell me if you want" or "if you want me to".
   Then the verdict is `stopped`, and the nudge is still sent.
5. `repeated`. The last 3 ledger events, not counting `supervise`, have one
   issue and one status. The loop is flagged. No nudge. Rule 4 comes first.
6. `ok`. None of the rules above match.

Work left is the count of `ready` issues from `lupin roadmap` (open, not
claimed, not blocked). It adds the count of open PRs that are not drafts. An
open PR that waits for the owner to merge it also counts. So `--every` does not
end while a repo has one. If Redis refuses the claim read with a permission
error, each issue counts as not claimed. The event says `claims: unreadable`. If
a count cannot be read, work left is unknown. Then rules 2 and 4 do not apply.
Also `--every` does not end. The PR count uses the shared `gh` cache key
`open-prs`, which only `supervise` fills. If `LUPIN_GH_CANONICAL_FETCHER` names
a machine that does not run `supervise`, the other machines cannot read the
count.

The nudge is a fixed text that starts with `Supervisor:`. Lupin sends it to the
loop's pane ID with `herdr agent prompt`. A loop with no pane ID gets no nudge.
Before it sends, the supervisor takes the loop's lock without waiting. If `lupin
stop` holds the lock, it skips the nudge (`nudge=busy`). Under the lock, it
reads the loop state again and runs `herdr agent get` on the pane. It skips the
nudge (`nudge=stale`) if the loop is not `running` in the same pane. It also
skips the nudge if the agent is not `idle` or `done`. Only one pass runs at a
time. A second pass prints "another supervise pass is running" and exits 0. A
loop gets at most one nudge try in 3600 seconds. A failed try counts too. If the
clock goes back, a stored time in the future counts as recent. The time must be
under 3600 seconds ahead.

The supervisor writes one `supervise` event to the ledger for each repo. The
event has no pane or handoff text. The supervisor module calls only `systemctl
is-active`, `herdr agent prompt`, and `herdr agent get`. It calls `gh pr list`
through the shared `gh` cache. It also uses Lupin's read functions, which run
read-only `herdr`, `gh`, and `git` commands. It keeps a hash of each pane, and
the time of each nudge try, in `$XDG_STATE_HOME/lupin/supervise.json` (default
`~/.local/state/lupin/supervise.json`).

Without `--every`, `supervise` makes one pass. With `--every`, it makes a pass
every SECONDS (default 3600, minimum 600). It ends on the first pass with no
work left. With no loops on the machine, it prints "no loops on this machine"
and exits 0. `--dry-run` sends no nudge and writes no ledger event, state file,
or lock file. It shows `nudge=would-be-stale` when the loop is not `running` in
the same pane. It still reads Redis for claims, the ledger, and the `gh` cache.
Exit code 3 means a ledger event was not written. Redis was unavailable or
refused the login. A host timer can run `lupin supervise` once an hour. This
repo does not ship one.

On the dispatch host, the systemd timer runs `fleet-run` every 5h15m. The
dashboard can start or stop that timer. The dispatch host has no Herdr and runs
no loops. Use the dashboard for all machines, and Herdr on a worker for its
loops.

`loops` reads Herdr's agent list and Lupin's saved loop state. Saved state stays
visible without Herdr. `idle` and `working` mean `running`. `blocked` means
`needs_attention`. `peek` reads a found agent pane, and reports the backend,
session, workspace, and pane IDs. It does not infer state from pane text.

Each machine has one Herdr session, `lupin-loops`, and one Herdr server. Each
run has one workspace in that session. Each workspace gets its repo's GitHub
token as `GH_TOKEN`. `stop` closes the workspace and leaves the session running.
The loops on a machine share the server's memory. A completed workspace stays
open for review, and it blocks another run until you stop it. A Herdr state of
`needs_attention` means the workspace needs review. If Herdr cannot provide
state, Lupin reports `unknown`. Both states block a new run until resolved.

`run`, `stop`, `peek`, `schedule`, `pause`, and `resume` can target any fleet
machine. Local actions run through Lupin. Remote actions use the signed Redis
queue, and wait up to `--wait` seconds (default 20). The target must run `lupin
agent` with Redis access and its own signing key. The sender also needs that
key. Inject it only into the Lupin process, through protected secret management.
Do not pass it in command arguments or save it in global environment settings.
Exit code 4 means the result is unknown after the wait. Check it with `lupin cmd
status <id>`. `stop`, `peek`, and `attach` need `--machine` when Lupin cannot
find one machine for the repo.

`stop` asks the agent to run `/handoff` before it closes anything. It waits up
to 10 minutes for the agent to finish. Then it saves a report, closes the
workspace, and stops the worker. Then it removes the run's worktree and branch
if git allows it (see "State"). If the wait ends first, `stop` continues and
says so. If Herdr cannot send the request, `stop` closes nothing and exits
non-zero. One cause is an agent that waits for an answer. Use `--force` to stop
at once, without a handoff. A remote `stop` can take longer than `--wait`. Then
it exits with code 4. The dashboard Stop button always uses `--force`.

`attach` never uses the queue. It opens Herdr here, or connects to the remote
Herdr server over SSH. `--print` shows the command instead of running it. Remote
attach needs an SSH target in `~/.config/lupin/ssh-targets`, one `<machine>
<target>` pair per line. Blank lines and `#` comments are ignored. Add one line
for each worker. Do not add the dispatch host.

```
worker-a  user@host-a
worker-b  user@host-b
```

Herdr and SSH key access must work on both machines. A missing target makes
`attach` fail. To use these machines in Herdr too, run `herdr machine add
<target>` for each one. `herdr machine list` shows the saved machines.

Debriefs stay as files on the worker that wrote them. The agent also puts each
debrief in Redis, with a digest. A digest is a short summary that a model writes
(`src/lupin/digest.py`). `lupin route prose size-s` picks the model. A digest
says when data is missing. It never calls missing data an empty result. A period
with no change gets no digest, and the page says "no changes". A digest is a
short story with up to 3 pictures, not a PR list. Periodic debriefs use fixed
windows on the Los Angeles clock. The first machine to claim a window in Redis
writes it. Only the 12-hour and 7-day windows get a digest. The other debriefs
(6h, 24h, loop stops) are records only. The Debriefs page (`/debrief`) shows the
debriefs of all workers. Redis holds text only. A screenshot file stays on its
worker. A GitHub attachment image is shown through the dashboard.

The digest uses these models in order: bmo (`bmo:qwen3.8-flash-next`, tier0 in
`model-tiers.json`), then `opencode-go/deepseek-v4.1-flash`, then Claude
(`haiku`). The worker reserves the fleet `bmo` slot and waits up to 300 s. A
failed or out-of-quota model moves to the next one. Quota and rate-limit errors
do not use a try. Other failed model calls count, with 3 tries at most. If every
model is blocked, the debrief shows with no digest, and Lupin tries again later.

`fetch-benchmarks` gets public benchmark scores. It does not run models. The
lookup worker comes from the newest model snapshot. It uses the opencode-go
models with input and output prices both zero, sorted by ID, up to four. Each is
tried in turn, and the first that answers does the work. With no zero-price
model, the cheapest priced model is used, and one line says so. With no
snapshot, the run stops with one line that says to run `lupin fetch-models`. The
worker runs through `omp -p` with maximum thinking. It reads each model's
Artificial Analysis page for the Intelligence Index score. It falls back to web
search for an unlisted model. Models go in calls of 6, up to 2 at a time. Each
score has its own timestamp. A refresh asks only about models whose score is
missing, unscored, or older than 20 hours. A failed call loses only its own
models. A lookup that finds nothing does not erase a verified score. `--force`
fetches everything now. Model IDs come from the latest `fetch-models` snapshot.
If it is missing or empty, Lupin uses the active picks in `model-tiers.json`.
The agent gets zero-price model IDs only when a live snapshot shows zero input
and output cost.

The Models page shows public scores by task category, for exact model IDs only.
It never copies a provider-family score to each model. A missing cell means no
verified score is available. The Notes column shows source-backed public
observations. The page shows no private test results and runs no private model
tests. "Perf" is each model's average score as a percent of the best score in
each benchmark. It averages equally across scored categories. "Value" is Perf
divided by the mean input and output price.

`quota` reads provider quota on machines with provider access, and publishes it
to shared Redis. The `/usage` page reads these shared rows. It also reads each
machine's local 7-day token and cost totals, from separate per-machine Redis
snapshots. It marks a snapshot stale after five minutes. A snapshot expires
after 24 hours. Systemd runs `lupin quota` every five minutes on each worker.

For opencode-go, Lupin looks for the API key in this order:
`~/.local/share/opencode/auth.json`, the `OPENCODE_API_KEY` variable, then the
output of `omp token opencode-go` (10 seconds at most). Lupin never prints or
logs the key. A provider with no cached usage rows shows its note instead.
Examples: "OpenCode Go API key expired" and "no OpenCode Go API key available".
`lupin quota` shows the note in plain output and in `--json`. It does not
publish a provider that has only notes.

A provider can have more than one login, for example a free and a paid OpenAI
account. `omp usage` reports each login. When a provider has more than one
login, Lupin adds an account label. The label is the plan name (`free`, `plus`).
If two logins share a plan, the label also has a short hash of the email and
account ID. The `/usage` page and `lupin quota` show the label. Lupin never
shows an email or account ID. `omp` chooses the login for each session. So a
provider is blocked only when all its logins are blocked. Lupin never selects a
blocked login. It selects the open login with the most quota left. A machine
that publishes a provider replaces the whole entry for that provider. Each
machine that publishes it needs every login for it.

Versions of Lupin before issue #169 write quota rows with no labels, to a
different Redis key. The `/usage` page shows those rows as stale, and a loop
start does not use them. Update every machine that runs `lupin quota`. Until you
do, a loop start can refuse a provider that only an old machine publishes.

## How a slot's limit (`max`) works

A slot does not know its limit until something tells it. The first `acquire`
call for a new slot sets the limit from `--max` (default 1). Every later
`acquire` call keeps the stored limit, and `--max` on a later call does nothing.
`status` reads the same stored limit.

## State

The `local` backend stores slot state as files, under `$LUPIN_STATE_ROOT` or
`~/.lupin/slots/` by default. Each slot is one directory. Each active holder is
one file in it, with a PID and an expiry time. The `redis` backend
(`slots_redis.py`) covers leases that several machines must see. Its schema is
in `docs/redis-schema.md`.

Loop state is stored under `$LUPIN_LOOP_STATE_DIR`, or
`/var/lib/delegation-loop` by default. Lupin keeps enabled repos in `repos`, and
orchestrator profiles in `orchestrators.json`. It keeps loop metadata in
`herdr-loops/`, run prompts in `notes/`, and stop reports in `reports/`. It
keeps run logs in `logs/`, run worktrees in `worktrees/`, and one-off schedules
in `once/`. `locks/` serializes local start and stop actions. Herdr keeps its
own session and workspace state.

Each run has a unique ID in `herdr-loops/<repo>.json`. Lupin writes events as
one JSON object per line, to `logs/<repo>/<run-id>.jsonl`. `lupin logs <repo>`
shows the latest 50 events of the latest run, in time order. `--run ID` selects
a run, `--tail N` sets the count, and `--json` prints one JSON object per line.
Events cover route choices, quota headroom, slot waits, acquisitions, and
releases. They also cover Herdr server changes, state changes, agent exits,
handoff results, CodeGraph index results, errors, and stop results. Lupin keeps
at most 20 logs per repo and removes logs after 30 days. A log write failure
does not stop the loop. Stop reports use the same run ID in their file names.

The dashboard caches GitHub data in `~/.local/state/lupin/cache.json`. The file
also holds each checkout's repo identity. If `gh` is missing, Lupin reads the
owner and repo name from a standard `origin` URL. It uses that name for claim
keys, even when the checkout folder has another name.

`gh_cache.py` shares read-only `gh` results across the fleet through Redis.
`LUPIN_GH_CANONICAL_FETCHER` names the one machine that runs `gh` on a cache
miss. Set it to that machine's hostname on every machine that reads the cache.
The named machine also fetches when Redis is down. When the variable is unset,
no machine is pinned. A machine fetches on a miss only while Redis is reachable.
It also needs the lock for that cache key. A machine that cannot get the lock
reads the cache again. If the cache is still empty, it returns a "fetch in
progress" error and does not fetch. The lock is a plain Redis key with a TTL,
not a slot. It does not show on the Machines page.

Each run has its own Git worktree in `worktrees/<repo>/` in the loop state
directory. A worktree is a second working copy of the repo, with its own files
and branch. This one is on a new branch, `lupin-loop/<time>`. `run` runs `git
fetch origin` first. The branch starts at the remote default branch: the target
of `origin/HEAD`, or else `origin/main`. If the fetch fails,
`run` refuses to start. The main checkout in `LUPIN_LOOP_CODE_DIR` is never the
agent's working directory.

The main checkout can have a CodeGraph index (`.codegraph/codegraph.db`). Then
`run` builds an index in the new worktree with `codegraph init -y`. Git does not
copy the index. `run` skips this step when `codegraph` is not on `PATH`, or when
git does not ignore `.codegraph/codegraph.db`. A failed index does not stop the
run. `run` writes a `codegraph_index` event with the status (`indexed`,
`skipped`, or `failed`) and the reason. A repo with no index in the main
checkout gets no event. A resumed run keeps its worktree and its index.

`run` removes the previous run's worktree and branch before it adds a new one.
If git keeps that worktree, `run` refuses to start and names its path. With
`--resume`, `run` reuses the worktree that the last run recorded, and never
creates one. If that worktree is missing, or is on another branch, `run` refuses
and names the path. If `run` fails after it adds a worktree, it removes that
worktree and branch. If git keeps it, the error names its path. If the platform
slot is full when the agent launches, the loop removes its worktree at once.

`run` refuses to start when the main checkout has a merge, rebase, cherry-pick,
or revert in progress. It also refuses when the checkout has unmerged files. The
error names the state and the path. Lupin never aborts, resets, stashes, or
cleans the checkout. Fix the checkout by hand, then run again.

`stop` removes the run's worktree, then its branch. Git refuses to remove a
worktree that has modified or untracked files. Then `stop` keeps the worktree
and prints its path. `stop` keeps the worktree when its commit is on no branch.
`stop` never passes `--force` to git and never deletes a branch with `-D`.

Before removal, `stop` copies two files from the worktree into `handoffs/` in
the loop state directory. `.loop/loop-state.json` becomes `<repo>.json`, and
`HANDOFF.md` becomes `<repo>.HANDOFF.md`. A file the worktree does not have
keeps its earlier copy. The next `run` prompt names each copy that exists. Git
deletes the other ignored files in the worktree, and Lupin does not keep them.

After removal, `stop` runs `git branch -d` on the run branch. Git refuses when
the checkout HEAD does not contain the branch tip. The branch then stays, and
`stop` prints its name. Delete it by hand after its work is merged. `git -C
<checkout> worktree list` shows kept worktrees.

`/roadmap` returns the page shell at once, and fills in the board or list from
`/roadmap/board`. That route renders one HTML fragment per query. It keeps the
last results for `LUPIN_ROADMAP_FRAGMENT_TTL` seconds (default 30). `?full=1`
renders the whole page in one response, with no JavaScript.

The Repos and Roadmap pages read the checkouts in `LUPIN_LOOP_CODE_DIR` (default
`/code`), on the machine that runs `lupin serve`. A repo with no checkout there
does not appear. The serving user must be able to read each checkout. If another
user owns a checkout, Git reports "dubious ownership", and the page shows no
issues. Add the checkout to Git's `safe.directory` list.

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
