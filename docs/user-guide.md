# Lupin user guide

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
`category size`). When quota blocks a route, Lupin first tries other choices
in that tier, then lower tiers. It uses a fallback only when a live model
snapshot less than seven days old shows that the fallback's input, output, and
listed cache prices are no higher than the blocked choice. It skips `bmo:` and
`local:` models as quota fallbacks. It does not use a higher tier to avoid a
quota wait. If no allowed model has quota, `route` prints a reason and a wait
estimate when it can calculate one. `--no-bmo` skips a tier-0 `bmo:` choice.
Add `--json` for a JSON object. A deferred route includes `reason` and
`wait_seconds`. If the category or size is not known, `route` and
`review-route` print one line to stderr with valid values and exit 2.

`pr-status` shows up to 100 open pull requests from one GitHub request. It
shows merge state, checks, reviews, approval, claims, comments, descriptions,
and requested reviewers. It does not fetch more than 100 comments or reviews
per pull request. If Redis cannot provide claims, the claim column says
`unknown` and the command prints a warning.

`ledger append` stores an event in Redis with the time and host. Repeat
`--child` for each split issue. Repeat a list option, such as `--highlights`,
to add more items. `ledger read --json` returns the latest 10 events, oldest
first. `--limit N` sets another positive count. A bad event is skipped if a
field is not valid UTF-8, a required field is missing, or a list field has
invalid JSON or nests more than 8 levels. Lupin reports each skipped event on
stderr. It still counts toward the limit. Both commands exit 3 when Redis is
unavailable. The Roadmap page then shows a warning and no ledger notes. The
roadmap ignores `.loop/loop-state.json`.

`review-route --prefetch` fetches the body and comments of each issue or PR.
For a PR, it also fetches reviews and a diff stat. The stat has file names,
additions, and deletions, but no diff text. By default, the command prints one
Markdown section per item. With `--json`, it prints only the fields a worker
uses: `kind`, `body`, `comments`, `reviews`, and `diff_stat`. Comments include
the author, time, and body. Reviews include the author, state, time, and body.
A number that is not an issue or PR gets an `error` in its own result.

`dispatch OWNER/REPO#N` claims the issue or PR and writes a Markdown brief in
the loop state directory's `notes/` folder. The brief has its body and every
comment. For a PR, it also has reviews and changed files. Then `dispatch`
starts a loop with the brief. `--no-start` writes the brief but does not start
the loop; the claim stays with the holder. `--json` prints the brief path and
the claim. If the brief is over 8,000 characters, the loop gets a short note
that names the file. The brief is not committed.

`acquire` prints a lease ID. Exit code 2 means the slot is full. Exit code 3
means Redis is unavailable and the command has no fallback. With the Redis
backend, only `bmo` uses the local backend if Redis is unavailable. When
Redis is unreachable, `status` uses local slot status and prints a warning.

A refused Redis login or an ACL denial does not fall back. Slot commands exit
3 and name the setting to check. A refused login names `--redis-password` or
`LUPIN_REDIS_PASSWORD`; fleet commands that reject the login also name the
systemd credential `redis-password`. Lupin never prints the password. The
local slot backend does not exit 3.

`hold` gets a lease, or reuses one from `--lease`. It runs `<command>` and
renews the lease while the command runs. It releases the lease when the command
ends, by normal exit, non-zero exit, or signal.

`fetch-models` checks which model IDs each subscription can call and their
prices. It saves the result to
`~/.local/state/lupin/model-snapshot.json`, unless `--no-write` is set. It
also publishes the snapshot to Redis. A Redis error does not remove the local
file and prints a warning. For OpenCode Go, Lupin checks the API key in the
same order as `quota` (see below). An expired key makes the row say
"OpenCode Go API key expired". For Codex, it uses `omp models` when available.
Otherwise it uses the public models.dev catalog and marks the rows `live:
false`. Lupin matches prices from models.dev by model ID. Codex prices come
from `omp models` when that command succeeds. A missing price is `null`.
`promo` is always `null`. If a fetch fails, Lupin keeps the last verified
list, marks it `live: false`, and shows the reason on the Models page.

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


`supervise` checks this machine's Lupin loops. It uses fixed rules and no
model. For each loop, it reads the Herdr state of its pane, the systemd unit,
the last 20 pane lines, and the newest handoff from this run. If Herdr does
not list the loop's pane, its state is `unknown`. The first matching rule
sets the verdict:

1. `stopped`, reason "waiting for the owner": Herdr says `blocked`, or Lupin
   says `needs_attention`. The loop is flagged. No nudge.
2. `stopped`: work remains and the systemd unit is `inactive` or `failed`.
   The loop is flagged. A nudge is sent only when Herdr says `idle` or `done`.
3. `busywork`: Herdr says `working`, and the pane text matches the last pass.
   The loop is flagged. No nudge.
4. `idle`: work remains and Herdr says `idle` or `done`. A nudge is sent. If
   the pane or handoff contains "tell me if you want" or "if you want me to",
   the verdict is `stopped`, but the nudge is still sent.
5. `repeated`: the last three ledger events, excluding `supervise`, have the
   same issue and status. The loop is flagged. No nudge. Rule 4 comes first.
6. `ok`: no other rule matches.

Work left is the number of `ready` issues from `lupin roadmap` (open, not
claimed, and not blocked), plus open pull requests that are not drafts. A pull
request that waits for a person to merge it still counts. So `--every` keeps
running while a repo has work. If Redis denies a claim read, each issue counts
as not claimed and the event says `claims: unreadable`. If Lupin cannot read a
count, work left is unknown. Rules 2 and 4 then do not apply, and `--every`
does not stop. The shared GitHub cache key `open-prs` holds the PR count.
Only `supervise` fills this cache. Other machines can read the count until
it expires. If no count is cached or it has expired, they cannot refresh it
while the pinned cache machine is online but not running `supervise`.

The nudge starts with `Supervisor:` and goes to the loop's pane. A loop with
no pane ID gets no nudge. Before sending, Lupin takes the loop lock without
waiting. If `lupin stop` holds it, the nudge is skipped (`nudge=busy`). Under
the lock, Lupin reads the loop state and pane again. It skips the nudge
(`nudge=stale`) if the loop is not running in the same pane or the agent is
not `idle` or `done`. Only one pass runs at a time. A second pass prints
"another supervise pass is running" and exits 0. Each loop gets at most one
nudge try in 3,600 seconds. A failed try counts. If the clock moves back, a
saved time less than 3,600 seconds in the future still counts as recent.

Lupin writes one `supervise` event to the ledger for each repo. The event
does not include pane or handoff text. It saves pane hashes and nudge times
in `$XDG_STATE_HOME/lupin/supervise.json` (default
`~/.local/state/lupin/supervise.json`). The command reads Herdr, systemd,
GitHub, and Redis data. These reads can update shared caches.

Without `--every`, `supervise` makes one pass. With `--every`, it makes a pass
every SECONDS (default 3,600; minimum 600). It stops after a pass with no work
left. With no loops, it prints "no loops on this machine" and exits 0.
`--dry-run` sends no nudge and writes no `supervise` event, state file, or
lock file. Other reads can still update shared caches. It shows
`nudge=would-be-stale` when the loop is not running in the same pane. Exit
code 3 means Redis blocked a ledger write.


`loops` reads Herdr's agent list and Lupin's saved loop state. Saved state
stays visible without Herdr. `idle` and `working` mean `running`. `blocked`
means `needs_attention`. `peek` reads a found agent pane and reports the
backend, session, workspace, and pane IDs. It does not infer state from pane
text.

Each machine has one Herdr session, `lupin-loops`, and one Herdr server. Each
run has one workspace in that session. `stop` closes the workspace and leaves
the session running. The loops on a machine share the server's memory. A
finished workspace stays open for review and blocks another run until you
stop it. `needs_attention` means the workspace needs review. If Herdr cannot
provide state, Lupin reports `unknown`. Both states block a new run until
resolved.

`run`, `stop`, `peek`, `schedule`, `pause`, and `resume` can target any fleet
machine. Local actions run through Lupin. Remote actions use the signed Redis
queue. The target must run `lupin agent` with Redis access, and the sender
must have the target's signing key. See [configuration](configuration.md) for
the key settings. Remote actions wait up to `--wait` seconds (default 20).
Exit code 4 means the result is unknown after the wait. Check it with
`lupin cmd status <id>`. `stop`, `peek`, and `attach` need `--machine` when
Lupin cannot identify one machine for the repo.

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

Debriefs stay as files on the worker that wrote them. Lupin also stores their
text and digest in Redis. A digest is a short summary from a model. `lupin
route prose size-s` picks the model. A digest says when data is missing; it
does not call missing data an empty result. A period with no change gets no
digest, and the page says "no changes". A digest is a short story with up to
three pictures, not a PR list.

Periodic debriefs use fixed windows on the Los Angeles clock. The first
machine to claim a window in Redis writes it. Only the 12-hour and 7-day
windows get a digest. The 6-hour, 24-hour, loop-stop, and backfill-day
debriefs are records only. The Debriefs page (`/debrief`) shows all workers'
debriefs. Redis can also hold copies of up to three GitHub attachment images
for seven days. Screenshot files stay on their worker.

The digest tries these models in order: bmo (`bmo:qwen3.8-flash-next`), then
`opencode-go/deepseek-v4.1-flash`, then Claude (`haiku`). It reserves the
fleet `bmo` slot and waits up to 300 seconds. A failed or out-of-quota model
moves to the next one. Quota and rate-limit errors do not count as tries.
Other failed calls count, up to three tries. If every model is blocked, the
debrief appears without a digest, and Lupin tries again later.

`fetch-benchmarks` gets public model scores. It uses the newest model
snapshot to pick a lookup worker: up to four zero-price OpenCode Go models,
sorted by ID. It tries each in order and uses the first that answers. If no
zero-price model is available, it uses the cheapest priced model and prints a
line to say so. If there is no model snapshot or no OpenCode Go model, it
prints a line that says to run `lupin fetch-models`.

The worker runs through `omp -p` with maximum thinking. It reads each model's
Artificial Analysis page for the Intelligence Index score. If the page read
fails or gives no score, it uses web search for that model. It checks models
in groups of six, with up to two groups at a time. Each score has its own
timestamp. A refresh asks only about models with no score or a score older
than 20 hours. A failed group keeps any earlier scores for its models; it
does not remove other scores. A lookup that finds no score does not erase a
verified score. `--force` fetches every score again. Model IDs come from the
latest `fetch-models` snapshot. If it is missing or empty, Lupin uses active
model picks from `model-tiers.json`. The prompt gets zero-price IDs only
when a live snapshot confirms both input and output price are zero.

The Models page shows public scores by task category, for exact model IDs.
It does not copy a provider-family score to each model. A missing cell means
no verified score is available. The Notes column shows public observations
with sources. The page shows no private test results and runs no private
model tests. "Perf" is each model's average score as a percent of the best
score for each benchmark. It averages equally across categories with scores.
"Value" is Perf divided by the average input and output price, in points per
dollar.

`quota` reads provider quota on machines with provider access, and publishes it
to shared Redis. The `/usage` page reads these shared rows. It also reads each
machine's local 7-day token and cost totals, from separate per-machine Redis
snapshots. It marks a snapshot stale after five minutes. A snapshot expires
after 24 hours. The default systemd timer runs `lupin quota` every five
minutes. Host config can change the interval.

For OpenCode Go, Lupin looks for the API key in this order:
`$XDG_DATA_HOME/opencode/auth.json` (default
`~/.local/share/opencode/auth.json`), `OPENCODE_API_KEY`, then
`omp token opencode-go` (up to 10 seconds). Lupin never prints or logs the
key. If there are no cached usage rows, the provider shows a note. Examples
include "OpenCode Go API key expired" and "no OpenCode Go API key available".
`lupin quota` shows the note in plain output and `--json`. It does not publish
a provider that has only notes.

A provider can have more than one login. `omp usage` reports each login.
When there are several logins, Lupin adds an account label. The label is the
plan name (`free`, `plus`). If two logins share a plan, the label also has a
short hash of the email and account ID. `lupin quota` and the `/usage` page
show the label, not the email or account ID. `omp` chooses a login for each
session. A provider is blocked only when all its logins are blocked. Lupin
uses an open login with the most quota left. A machine that publishes a
provider replaces that provider's full entry. Each machine that publishes a
provider needs all its logins.

Quota rows without account labels appear as stale on the `/usage` page. Loop
starts do not use them. Update every machine that runs `lupin quota`.

## How a slot's limit (`max`) works

A slot does not know its limit until something tells it. The first `acquire`
call for a new slot sets the limit from `--max` (default 1). Every later
`acquire` call keeps the stored limit, and `--max` on a later call does nothing.
`status` reads the same stored limit.

## State

The `local` backend stores slot state as files, under `$LUPIN_STATE_ROOT` or
`~/.lupin/slots/` by default. Each slot is one directory. Each active holder
is one file with a process ID and an expiry time. The `redis` backend shares
leases across machines. Its schema is in the [Redis schema](redis-schema.md).

Loop state is stored under `$LUPIN_LOOP_STATE_DIR`, or
`/var/lib/delegation-loop` by default. Lupin keeps enabled repos in `repos`,
profiles in `orchestrators.json`, loop metadata in `herdr-loops/`, prompts in
`notes/`, reports in `reports/`, handoff copies in `handoffs/`, logs in
`logs/`, worktrees in `worktrees/`, one-off schedules in `once/`, debriefs in
`debriefs/`, and timer settings in `timer-dropin.d/override.conf`. `locks/`
serializes local start and stop actions. Herdr keeps its own session and
workspace state.

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

The dashboard shares read-only GitHub results through Redis. If
`LUPIN_GH_CANONICAL_FETCHER` names a machine, that machine fetches cache
misses while Redis is reachable. Other machines use the cache while the
named machine is online. If it is offline, draining, or has not joined, a
different machine can fetch. If Redis is down, only the named machine can
fetch. With no named machine, a miss uses a per-key lock. One machine fetches;
others read the cache again and, if it is still empty, report that a fetch is
in progress. The lock is a Redis key with a time limit, not a slot. It does
not appear on the Machines page.

Each run has its own Git worktree in `worktrees/<repo>/` in the loop state
directory. A worktree is a second working copy of the repo, with its own files
and branch. This one is on a new branch, `lupin-loop/<time>`. `run` runs `git
fetch origin` first. The branch starts at the remote default branch: the target
of `origin/HEAD`, or else `origin/main`. If the fetch fails,
`run` refuses to start. The main checkout in `LUPIN_LOOP_CODE_DIR` is never the
agent's working directory.

The main checkout can have a CodeGraph index (`.codegraph/codegraph.db`).
`run` then builds an index in the new worktree. It skips this step when
`codegraph` is not on `PATH`, or when Git does not ignore the index file. A
failed index does not stop the run. Lupin writes a `codegraph_index` event
with the status (`indexed`, `skipped`, or `failed`) and the reason. A repo
with no index in the main checkout gets no event. A resumed run keeps its
worktree and index.

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

