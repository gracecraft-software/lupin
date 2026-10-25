# lupin CTL: copy

Existing commands are kept as given: `status`, `repos`, `sessions`, `enable`, `disable`, `run`, `once`, `schedule`, `pause`, `resume`, `stop`, `peek`, `attach`, plus `join` from the mockups. New commands are `roadmap`, `place`, and `quest`. `place` is new: it is not
the existing `lupin route <category> <size>` (which still only picks a
`{model, effort}` for a task — unchanged). `place` takes a task already
matched to a model and decides which machine runs it. Marked **(proposed)**: those, `--dag`, `--json`, and optional `repo[#N]` on `pause`/`resume`. Rename freely.

---

## 1. Model-facing section (system prompt / tool description)

```
lupin is the control CLI for the delegation loops on this machine group. Use it to
read the roadmap, choose where a task should run, and control loops. Run it through
the shell. Every command is safe to re-run unless noted.

Read before you act:
  lupin status                 what is running, what is scheduled, quota per provider
  lupin roadmap [--dag]        prioritized open tasks; --dag shows dependencies and blockers
  lupin quest                  quests, their progress, and which machine is focused on each
  lupin place <task>           which machine runs a task, and why

Act:
  lupin run <repo> [--loop N]  start work now
  lupin once <when> [repo…]    run one time at 15:00, +2h, or tomorrow 09:00
  lupin schedule               show or change the recurring cadence
  lupin quest focus <quest-name> [--machine M]   dedicate one machine to a quest
  lupin quest release <quest-name>               end the focus early (normally automatic)
  lupin quest start --issue N [--issue N…]   ship a few specific issues with one dedicated loop
  lupin quest status|stop [id]
  lupin pause [repo[#N]]       pause the recurring timer, or one loop
  lupin resume [repo[#N]]      resume it
  lupin stop <repo>[#N]        kill a loop session and release its task

Watch:
  lupin peek <repo> [lines]    tail of a loop session
  lupin attach <repo>          attach to a loop session

Rules:
- Check `lupin place` before `lupin run` when more than one machine could take the task.
- Prefer the provider with the most quota left that still fits the task type. Do not spend the last 10% of any provider on a task that can wait for a reset. Low quota only affects where new work goes. It never releases a focus or a claim.
- Quests ship together. When a task belongs to a quest, run it in that quest's order and on that quest's focus machine if it has one. Use `lupin roadmap --dag` to see which tasks block others, and start with those.
- Use `quest start` when the user names specific issues to ship. Use `quest focus` when the issues already share a quest label. A quest claims its issues up front and works them in dependency order.
- Focus and claims release themselves. Do not release them by hand unless the user asks. Use `--pin` only when the user wants a focus to stay.
- Tasks, quests, priorities and dependencies come from GitHub issues. Fix them in GitHub, not in lupin.
- Do not split a quest across machines unless its remaining tasks are independent in the DAG and the focus machine has no free slot.
- Tasks claimed by another loop are not yours. Skip them.
- `pause` keeps the loop's claimed task and resumes where it stopped. `stop` kills the session and releases the task back to the queue. If unsure, pause.
- With no repo, `pause` and `resume` act on the recurring timer for all repos. Name a repo to affect only its loop.
- To change when work runs, use `once` for a single run and `schedule` for the recurring cadence. Use `peek` to check a loop before you pause or stop it.
- Add `--json` for output you will parse. Without it, output is for people.
- Exit code 0 means done. 2 means nothing to do. Any other code means the command failed; read stderr before retrying.
```

---

## 2. `--help`

```
lupin <command> [options]

Inspect
  status                     enabled repos, live sessions, timers, quota
  repos                      every repo under /code and its loop state
  sessions                   the newest agent session file per repo
  roadmap [--repo R] [--dag]  prioritized open tasks            (proposed)
  quest [--json]              quests and their progress           (proposed)
  place <task> [--explain]   best machine for an already-routed task   (proposed)

Schedule
  enable <repo> [--platform P]   add a repo to the scheduled set
  disable <repo>                 remove a repo from the scheduled set
  run [repo] [--loop N] [--note TEXT] [--platform P]
                                 start work now
  once <when> [--note TEXT] [--platform P] [repo…]
                                 start loops later, one time (15:00, +2h, tomorrow 09:00)
  schedule [...]                 show or change the recurring cadence
  pause [repo[#N]]               stop the recurring timer, or one loop (repo is proposed)
  resume [repo[#N]]              start it again
  stop <repo>[#N]                kill a loop session
  quest <action> [options]       focus, release, start, status, or stop a quest   (proposed)

Watch
  peek <repo> [lines]            print the tail of a loop session
  attach <repo>                  attach to a loop session

Machines
  join <coordinator>         add this machine to the group

Global options
  --json        machine-readable output
  --quiet       print only the result line
  -h, --help    show help for a command

Run `lupin <command> --help` for details and examples.
```

### Per-command

**roadmap**
```
lupin roadmap [--repo R] [--limit N] [--stage S] [--dag] [--json]

List open tasks in priority order. Tasks claimed by other loops are marked and
excluded from "ready" counts.

  --repo R     only this repo
  --limit N    show the top N (default 10)
  --stage S    ready, blocked, or all (default ready)
  --dag        draw dependencies between tasks, grouped by quest

Examples
  lupin roadmap
  lupin roadmap --dag
  lupin roadmap --repo api-gateway --limit 3 --json
```

**quest**
```
lupin quest [--json]
lupin quest focus <quest-name> [--machine M] [--pin]
lupin quest release <quest-name>
lupin quest start --issue N [--issue N…] [--machine M] [--platform P] [--note TEXT]
lupin quest status [id]
lupin quest stop <id>

A quest is a set of tasks meant to ship together — either an issue labeled `quest`
(its sub-issues are its tasks), or a set you name directly with --issue. Plain
`lupin quest` lists each quest with progress and its focus machine.

  focus        route a labeled quest's ready tasks to one machine, in dependency
               order. Without --machine, lupin picks the machine with the most
               free slots.
  release      return the machine to normal routing. Running loops finish their step.
  --pin        keep the focus until you release it; skip automatic release.
  start        claim the named issues up front and work them in dependency order,
               under one dedicated loop. Issues can come from different repos.
  --issue N    an issue to ship; repeat for each (at least one)
  --machine M  run on this machine; default is the best fit from `lupin place`
  --platform P force a provider
  --note TEXT  extra instruction for the loop

A focus releases itself when it stops paying off; a started quest ends on its own
when every issue is closed or merged. See "How focus and claims end" below.

Examples
  lupin quest
  lupin quest focus session-rewrite --machine mac-studio
  lupin quest release session-rewrite
  lupin quest start --issue 23 --issue 24 --issue 25
  lupin quest status
  lupin quest stop q1
```

**place**
```
lupin place <task> [--exclude-provider PROVIDER ...] [--explain] [--json]

Pick a model and a machine for a task. It classifies the task and routes a
model, then ranks machines that can run it. It ranks by machine state, quest
focus, free slots, and heartbeat age. Provider quota is shared, so it does not
rank machines by quota.
<task> is an issue number (#418) or a short description.

  --exclude-provider P  avoid a provider; repeat to avoid more than one
  --explain             show the quota windows and why each machine lost
  --json                include quota.windows in the result

Examples
  lupin place "#418"
  lupin place "migrate session store" --exclude-provider claude --explain
```

**pause / resume**
```
lupin pause [repo[#N]]
lupin resume [repo[#N]]

With no repo, stop or start the recurring timer for all repos. Running loops finish
their current step. With a repo, pause only that loop: it stops after the current
step and keeps its claimed task. <repo>#N picks loop N when a repo has several.

Examples
  lupin pause
  lupin pause api-gateway#2
  lupin resume api-gateway#2
```

**stop**
```
lupin stop <repo>[#N]

Kill a loop session. Its task goes back to the queue and its branch and notes are
kept. Prefer `pause` if the work should continue later.
```

**schedule**
```
lupin schedule [options]

With no options, show the recurring cadence and the next run for each repo. Options
change the cadence. Use `once` for a single run instead.
```

**peek / attach**
```
lupin peek <repo> [lines]     print the last lines (default 40) of the loop session
lupin attach <repo>           attach to the live session; detach to leave it running
```

**once**
```
lupin once <when> [--note TEXT] [--platform P] [repo…]

Start loops later, one time. <when> accepts 15:00, +2h, or tomorrow 09:00. Times are
local to the coordinator. With no repo, every enabled repo runs.
```

**join**
```
lupin join <coordinator>

Add this machine to the group. Run it on the machine you are adding.
```

---

## 3. Where data comes from (GitHub)

```
Quest        an issue labeled `quest` (sub-issues are its tasks), or named directly with --issue
Priority     labels P1, P2, P3 (lower number first); unlabeled sorts last
Dependency   GitHub "blocked by" links
Done         issue closed, or its PR merged
Claimed      a loop has assigned itself the issue
```
lupin re-reads GitHub on every command and caches for 60s. `--refresh` forces a re-read.
Missing or broken data is reported, not guessed:
```
warning: api-gateway: 1 open issue has no priority label (sorted last)
warning: #431 depends on #999, which does not exist. Treated as unblocked.
```

## 4. How focus and claims end

**Quest focus releases when any of these is true**
```
done        every task in the quest is closed or merged
closed      the quest issue is closed
stalled     no ready task for 15m and the open ones wait on work outside the quest
idle        the focus machine has had free slots and no ready quest task for 30m
down        the focus machine is offline, draining, or disabled
```
Low quota never releases a focus. The focus machine keeps the quest and waits for the reset.
On `down`, focus moves to the next best machine when one can continue the quest, and ends only if none can.

**Focus is kept when**
```
- remaining tasks wait only on a quest task running on the focus machine
- quota is low or out; the quest waits for the reset on the same machine
- a short gap is expected: a blocker clears within 15m
- it was set with --pin
```
**A started quest ends when**
```
done        every issue is closed or merged
stopped     lupin quest stop
down        its machine is offline, draining, or disabled; the quest moves to the next best machine, or ends and releases its claims if none can
```
A quest's claims follow the same stale and lost rules as any claim. Low quota never ends a quest.

**Claims release when**
```
merged       the PR merged or the issue closed
lost         no heartbeat for 10m
stale        no commit or tool activity for 30m
stopped      lupin stop
```
Low quota never releases a claim. The loop waits for the reset and keeps the task.
A released task keeps its branch and notes, so the next loop continues from them. Every release prints one line with its reason, so models can tell what happened.

---

## 5. Output text

### `lupin roadmap`
```
api-gateway · 4 ready · 1 blocked

 1  #418  P1  retry backoff               ready
 2  #431  P2  rate-limit headers          ready
 3  #422  P2  split session store         claimed by api-gateway #2
 4  #451  P3  trace id propagation        claimed by billing-core #1
```
### `lupin roadmap --dag`
```
quest session-rewrite · 2 of 5 done · focus: mac-studio

  #410 ✓ ─┬─> #418 ● retry backoff ───┬─> #431 ○ rate-limit headers
           └─> #422 ◐ split store ─────┘        │
                                                 └─> #440 ✕ docs
  #451 ○ trace ids   (no dependencies, not in a quest)

✓ done  ● ready  ◐ claimed  ○ waiting  ✕ blocked
Blocking the most: #422 (unblocks #431, #440). Next ready on the critical path: #418
```
Cycle: `error: dependency cycle #431 -> #440 -> #431. Fix the links in the tracker.`

Empty: `No ready tasks in api-gateway. 3 are claimed by other loops, 1 is blocked.`
Empty everywhere: `No ready tasks in any repo. Nothing to run.`

### `lupin place "#418" --explain`

The first lines show the selected model and every quota window:
```
#418 retry backoff · code edit, small → opus high (claude)
claude quota:
  5 Hour limit: 62% left, resets in 3h 10m
  Weekly limit: 9% left, resets in 4d
```

The rest lists candidate machines and why each one won or lost. The JSON
result includes the same windows under `quota.windows`.

### `lupin quest`
```
session-rewrite   2/5 done   5 tasks, 3 open   focus: mac-studio (3 of 4 slots)
billing-v2        0/4 done   4 tasks, 4 open   no focus
```
```
focused session-rewrite on mac-studio · 3 ready tasks will route there in order
released session-rewrite · mac-studio returns to normal routing
```
Errors: `error: mac-studio is draining and cannot take a focus. Pick another machine.` and `error: billing-v2 has no ready tasks to focus on. #470 is blocking; see lupin roadmap --dag`

### Automatic releases
```
released focus session-rewrite · done · all 5 tasks closed
released focus session-rewrite · stalled · #431 waits on #502 outside the quest
moved focus session-rewrite to mini-2 · mac-studio is offline
kept focus session-rewrite · #431 waits on #422, running on mac-studio
released claim #422 · stale, no activity for 30m · back in the queue, branch kept
```
`lupin status` shows what will release next:
```
session-rewrite  focus mac-studio  releases when #431 and #440 close, or after 30m idle
```

### `lupin quest start`
```
quest q1 started on mac-studio · #23 #24 #25 claimed · order: #23, #25, #24
  #25 waits on #23 (blocked by)
```
```
q1 · mac-studio · 1 of 3 shipped
  #23 ✓ merged   #25 ● in progress   #24 ○ waits on #25
```
```
quest q1 done · #23 #24 #25 shipped · mac-studio is free
quest q1 stopped · #25 released to the queue, branch kept
```
Errors:
```
error: #24 is claimed by billing-core#1. Wait for it or stop that loop.
error: #25 is closed. Remove --issue 25.
error: #31 does not exist in any enabled repo.
error: #23 is blocked by #19, which is not in this quest. Add --issue 19 or wait for it.
error: mac-studio is draining and takes no new work. Pick another machine.
```

### `lupin pause` / `resume` / `stop`
```
paused timer · no new loops start · 3 running loops finish their current step
resumed timer · next run in 12m 04s
paused api-gateway#2 · keeps #422 · resume with: lupin resume api-gateway#2
resumed api-gateway#2 · continuing #422
stopped api-gateway#2 · released #422 to the queue, branch kept
```
Already in state: `api-gateway#2 is already paused. No change.`

### `lupin once` / `run` / `enable` / `disable`
```
scheduled api-gateway · runs once at 15:00 (in 2h 14m)
started api-gateway#1 on mac-studio · claimed #418
enabled web-client · added to the scheduled set
disabled web-client · its running loop finishes the current step, then stops
```

### Errors
```
error: no repo named "api-gatway". Did you mean api-gateway?
error: "tomorrow 25:00" is not a time. Use 15:00, +2h, or tomorrow 09:00.
error: api-gateway has 3 loops. Choose one: api-gateway#1, #2, or #3.
error: mac-studio is draining and takes no new work. Pick another machine or resume it.
error: cannot reach the coordinator. Check the network, then retry. Loops keep running.
error: #422 is claimed by billing-core#1. Pick another task: lupin roadmap
```

---

## 6. Example flows for models

**Fetch what to work on**
```
lupin roadmap --limit 3 --json
```

**Route, then run**
```
lupin place "#418" --json
lupin run api-gateway --loop 1
```

**Quota is nearly out: wait for the reset**
```
lupin place "#418"            # says claude resets in 3h 10m
lupin once +3h api-gateway
```

**Ship a quest together**
```
lupin roadmap --dag
lupin quest focus session-rewrite --machine mac-studio
lupin place "#418"            # routes to the focus machine
lupin quest release session-rewrite
```

**Ship named issues together**
```
lupin quest start --issue 23 --issue 24 --issue 25
lupin quest status
```

**Pause for a user, resume later**
```
lupin pause api-gateway#2
lupin resume api-gateway#2
```

**Hold all new work, then restart**
```
lupin pause
lupin resume
```

**Move a run**
```
lupin once tomorrow 09:00 web-client
```

**Check a loop before stopping it**
```
lupin peek api-gateway 20
lupin stop api-gateway#2
```
