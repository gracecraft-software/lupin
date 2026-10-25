---
name: superloop
description: >-
  Supervise Lupin loops on one machine, by hand or with lupin supervise. Use it when asked to supervise loops.
compatibility: >-
  Requires Lupin, Herdr, and the delegation-loop skill on the supervisor host. Redis is required for ledger records.
---

# Superloop

`lupin supervise` runs fixed rules and uses no model. An agent with this skill
handles steps that need judgment: a blocked loop, an alert, or an unclear reply.
Dynamic pricing, nuanced situation, or loops that require extra handholding may also need this.
Do not copy the rule text into other files. Copy the text inside the quotes of
`NUDGE_TEXT` in `<checkout>/src/lupin/supervise.py`. Do not copy the Python.

## What lupin supervise does

For each loop on this machine, `lupin supervise` gives one verdict. The rules
are `verdict` in `src/lupin/supervise.py`. It sends `NUDGE_TEXT` only when the
Herdr state is `idle` or `done`. It sends at most one nudge per repo each hour.
It writes one `supervise` ledger event per repo. A `stopped` verdict does not
block a nudge. An idle loop is `stopped` if its unit is inactive or if it asks
to continue. A `blocked` or `needs_attention` pane gets no text. Its verdict is
`stopped` with the reason `blocked, research needed`. The Debriefs page includes
the daily owner-blocked report.

## Decide: owner decision or not

Ask: can only a person supply or decide this?

| Owner decision: wait for the owner                 | Not an owner decision: research, then act                      |
| -------------------------------------------------- | -------------------------------------------------------------- |
| Credentials: a token, password, login, or key      | A rate limit, timeout, or other transient error                |
| Money: a paid plan, purchase, or budget            | A failed test, gate, push, or merge conflict                   |
| Hardware: a machine or device                      | A missing label or tool that the repo docs give a fallback for |
| A product or policy choice                         | A technical choice between options                             |
| An action that the repo rules reserve for a person | A model or quota limit (see "Hourly pass")                     |

If you are not sure, dispatch researchers first. A research worker answers one question.
It gathers information and writes an informed report. It does not change code, issues, pull requests, or loops.

## Blocked loop

1. Read the pane:

   ```sh
   herdr --session SESSION agent read PANE_ID --source recent-unwrapped --lines 120
   ```

2. If the pane shows `Awaiting owner input`, read the question. Apply "Decide".
3. Owner decision: write the question in an issue comment that starts with
   `Loop:` and names its options. Record `owner-decision` and put the question
   in the handoff.
4. For any other block, do not prompt the pane. The supervisor reports
   `blocked, research needed`. Record the question, pane ID, and time. Dispatch a
   research worker on the next pass and act on its result.

## Sending the rule by hand

Use this only when `lupin supervise` did not nudge the loop.

1. Run `lupin loops --json`. Take `pane_id` (PANE_ID) and `session` (SESSION)
   from the object whose `repo` is the loop's repo. If the loop is not listed,
   do not send. Report it in the handoff.
2. Send the text of `NUDGE_TEXT` as one prompt:

   ```sh
   herdr --session SESSION agent prompt PANE_ID "TEXT" --wait --timeout 120000
   ```

3. Check the reply with the read command above. The reply must name the block,
   the research result, and the next action.
4. If the reply is missing, send the rule once more. If the second reply also
   fails, record `blocked` and report it in the handoff.
5. If the send fails, times out, or returns `agent_prompt_stalled`, do not send
   again at once. Read the pane first.

## Hourly pass

Each hourly block in the monitor log starts with `=== hourly check`.
Check the burn rate.

1. Read the last 40 lines:

   ```sh
   tail -n 40 <monitor-log>
   ```

2. For `roundsmith`, `lupin`, and `bodysmith`, the block must show `running`.
   Each `ALERT` line is a block. Apply "Decide".
3. For an `ALERT`, read the pane with `lupin peek REPO 25`.
4. Check the burn rate with `lupin quota`; if a loop is burning through usage faster than the daily budget, adjust accordingly. Evenly spread out usage and budget 10% of weekly headroom for the owner to use at their discretion, _unless_ the weekly limit is 3 hours from resetting. Always try to use `bmo` or other free models as needed to triage, etc.
5. Occasionally inspect the workers to identify areas where ANY process could be streamlined or improved; proactively ticket anything you see that seems less than optimal. E.g. unpushed local changes, confusing or brittle instructions, wasteful spend, failure to trigger ui-pressure-tests, doc bloat, etc.

Do not start a model run unless you can read
`~/.local/state/lupin/model-snapshot.json`. If you cannot read it, record the
block.

## Record

Record each block, research result, deferred item, and missed pass:

```sh
lupin ledger append OWNER/REPO --event supervisor --status STATUS \
  --summary "TEXT" --evidence "TEXT" --decisions "TEXT" --next "TEXT"
```

- STATUS is one of: `blocked`, `research-done`, `owner-decision`, `deferred`,
  `missed-pass`, `stopped`.
- Add `--issue N` only for an item with an issue number (positive whole number).
- If the command exits with code 3, Redis is not reachable. Put the same text in
  the handoff and report it.

Each record must make sense alone. Read the record back:

```sh
lupin ledger read OWNER/REPO --limit 20
```

## Stop

There will always be work to do. The superloop ends when the owner tells you to stop.

However, after 24 hours, if you haven't been stopped, you may pass your session's superloop torch to the next superloop.

When you are wrapping up, record the end with status `stopped`. Run the `handoff` skill.
Include the last hourly pass time, each open block, and the executive summary.
