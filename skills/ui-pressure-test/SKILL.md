---
name: ui-pressure-test
description: >-
  Run deep, human-like end-to-end tests of a web app through its real interface.
  Use this skill for major user-facing changes, release checks, or requests to
  find bugs, confusing steps, missing features, and design friction across user
  journeys, input methods, and devices. It coordinates a director and several
  small-scope evaluator agents, tests the app through the UI and its real save,
  export, and service paths, and turns findings into prioritized tickets.
compatibility: Requires a running app and browser automation with visual evidence. Multiple agents and real touch devices are recommended.
---

# Pressure-test a web app

Test the app as a person would use it. Use the real interface first. Follow the
work into the app's services and saved data. Check both whether each task works
and whether people can understand and complete it.

Use a director and several evaluator agents for a substantial test. Keep each
evaluator's assignment small so it can inspect the screens and steps in detail.

## Set safe test conditions

1. Read the repo instructions. Find how to run the app, its test accounts, test
data, supported browsers and devices, and any existing journey or design notes.
2. Use a test or preview environment with isolated accounts and data. Do not use
production, real payments, real messages, or other irreversible actions unless
the user has authorized them. Do not send unbounded traffic. This is a user-flow
test, not a load test or a security test.
3. Start the app and confirm the target build, URL, and account. Capture the
starting state. If the app or a needed account is not available, ask only for
that missing access; test the parts that are available.
4. Check the available browser and device controls. Use real browser actions
and inspect the rendered screen. Do not infer a visual result from source code,
API responses, or a passing automated test.

## Keep the repo QA register

Find the repo's one QA register issue. Use an existing issue. Create one only
when none exists. In Lupin, use issue #88.

The register has one row per capability, journey, mode, or export. Use these
columns:

| Item | Devices | Last QA date | Commit tested | Result | Evidence | Changed since? |
|---|---|---|---|---|---|---|

List phone, tablet, and desktop as `tested` or `not run` in `Devices`. Use the
full commit SHA for `Commit tested`. Fetch origin before this check. For
`Changed since?`, list commits that changed the item's files after the tested
commit:

```sh
git log --format=%H <commit-sha>..origin/main -- <item-files>
```

Record `0` when this command returns no commits. Otherwise record the count
and newest SHA. Link the pull request comment with the screenshots in
`Evidence`. For a standing test, attach screenshots to a comment on the QA
register issue and link that comment. Do not use a local file path.

Update only rows that the test ran. Add a row for each new item. For a new item
that was not tested, leave the date, commit, and changed-since value blank and
set `Result` to `not run`. Leave other rows unchanged.

If a browser blocks a test, keep the existing row's date and result. Put the
blocked status and reason in the run report and the `pressure-test` ledger
event. A blocked attempt is not a completed test.

## Build the coverage plan

Start with all due rows from the register and the selected long-tail sample.
Add new items that you find in the app. Do not drop a register item because
this run will not test it.

Inventory the app's user-facing tasks and visible modes before testing. Use the
repo's docs and app navigation, then confirm the list against the running app.
Include all relevant items, such as:

- Sign-in, onboarding, create, edit, save, reopen, share, and delete flows.
- Every visible creation or editing mode, including alternate tools and
  shortcuts. For a 3D app, this may include primitives, sketch tools, direct
  manipulation, and transform tools.
- Every visible export type and its options. Download each one and check that
  the file opens or can be imported again. Check its contents, not only that a
  download started.
- Empty, normal, large, and invalid inputs; validation, loading, error,
  interruption, recovery, undo, and redo states where the app supports them.
- User roles, account permissions, or feature flags when they change what a
  person can see or do.
- Safe interruptions, such as a quick repeat action, refresh or Back during a
  save, a change of input method, a screen rotation, or a paused network.
- The input methods the app claims to support: mouse clicks, hover, wheel,
  right-click, keyboard-only use, touch, drag and drop, pen, or other modes.
  Do not claim that a method passed if the browser or device could not perform
  it.
- Supported screen sizes, orientation changes, and browsers. Every journey
  runs on all three device sizes. See "Run the three device passes".
- Use `ux-pressure-test` for accessibility, performance, and resilience checks.
  Name assistive tools only when you used them; keyboard checks are not a
  screen-reader test.

Create a coverage table before dispatch. Give every planned journey and
variation a status: `not started`, `passed`, `finding`, `blocked`, or `not run`.
For each item, record the device, browser, input method, account or data state,
and the expected result. The table has one row per journey and device. Do not
drop a journey, mode, or export to fit one agent's workload; add agents or run
another wave. Test each supported option.
For a large set of combinations, first test each option on its own, then test
the important combinations. List every combination not tested, with the exact
reason and a next step. Do not call the test complete while safe, available
items remain untested. Never mark an untried item as passed.

## Run the three device passes

Every journey runs on all three device sizes. These sizes are fixed:

- Phone: 390x844.
- Tablet: 768x1024.
- Desktop: 1440x900.

The coverage table has one row per journey and device. A journey that did not
run on a device is `not run`, with the exact reason. `not run` is not a pass.
Do not skip a device because the screen looks the same. A small screen often
breaks the parts a big screen hides.

Give each device pass to an evaluator, or run the passes in sequence. Keep the
journey steps the same on every device, so the difference is the size. Save a
screenshot of every screen on every device. These images are the evidence for
the visual review and the pull request comments.

## Find the core journey

Choose the most important user goal that crosses the main parts of the app. For
example: create an item, change it, save it, reload or reopen it, then export
it. Record the normal path and its expected result. Run this path once as the
baseline before splitting it into variations.

Test end to end: perform actions through the visible interface, then confirm
the result reached the real service or saved state. Reload or reopen the item
when that is part of the user's task. Check exported files and other visible
results. Use logs, network inspection, or data inspection only to confirm what
the UI did; do not use a direct API call to stand in for a user action.

## Dispatch evaluator agents

Use three or more evaluators when the agent runner supports them. Add more for
separate journeys or major tools; do not give one evaluator a large list just
to reduce agent count. If several agents are not available, work in small
separate passes and report that parallel evaluation was not available.

Start with the baseline journey. Then fan out its meaningful variations and
related journeys. Give each evaluator exactly one journey family and no more
than five paths, including the baseline if it must repeat it. Examples of
separate assignments:

- Same creation flow with mouse, keyboard-only, and touch input.
- Same flow on phone 390x844, tablet 768x1024, and desktop 1440x900.
- A different creation mode or tool path.
- One export family, with each available format and its reopen/import check.
- A recovery path, such as refresh, undo, interruption, and resume.

Use a separate browser context, account, or data set for each evaluator. Do not
let agents change the same project or session at the same time. If state cannot
be isolated, run those assignments in sequence. Keep the core steps the same
across a variation when possible; change one main factor at a time so failures
are easy to explain. Combine factors when an interaction between them is a
likely source of failure, such as touch plus a narrow screen.

Mark the QA task `ui-pressure-test` when the repo's label map supports it.
Use `frontend-ui` for its visual and interaction scope. For CAD apps, route
geometry or spatial-tool evaluator lanes as `cad-spatial`. The classifier
returns one category and checks CAD first. Split those lanes or route them
separately when both benchmark categories matter.

Give each evaluator a short brief with:

- The exact journey, starting state, and assigned variations.
- The target URL, build, account, test data, and safety limits.
- The device, viewport, browser, and input method to use. Give the phone,
  tablet, and desktop sizes when the evaluator runs all three passes.
- The expected result and any known project rule.
- Where to save screenshots, downloads, and other evidence.
- A limit of one journey family and five paths.

Ask evaluators to use the real UI and report only what they observed. They must
not edit app code, fix findings, or expand their assignment without telling the
director. Ask them to return a short report with the fields below.

Each evaluator returns one row per path with:

- Status: `passed`, `finding`, `blocked`, or `not run`.
- Device, browser, viewport, input method, and starting data.
- Steps completed and the result seen.
- A screenshot of every screen it visited, named with the device.
- Findings with repeat steps, user impact, and evidence links.
- Any assigned item not tested and the exact reason.

## Exercise the app

Use browser automation to click, type, scroll, drag, use keys, and take
screenshots. Inspect the actual screen after important actions. Use realistic
pace and pauses. Try mistakes and recovery, not random clicks with no user
goal.

For each assigned path:

1. Start from the stated state. Record browser, viewport, input method, and
   test data.
2. Follow the steps in order. Try the assigned variation. Note unclear words,
   hidden controls, extra steps, unexpected movement, slow or missing feedback,
   and errors that do not help the user recover.
3. Check saved state after a refresh or reopen when relevant. For a 3D app,
   inspect the shape from useful views and confirm scale or position if the task
   depends on them.
4. For each export, choose the format in the UI, download it, and inspect or
   re-import the file. Compare the result with the on-screen work.
5. Save evidence for each finding. Include the steps, screen, and relevant
   output. Keep private data out of screenshots and tickets. Take one
   screenshot of every screen on every device pass, and name each file with
   the journey, the screen, and the device.
6. Reset to a known state before the next variation. Report any test data that
   could not be reset.

Use touch input on a real device when one is available. Browser touch
emulation is useful, but label it as emulation. A resized desktop window is not
a mobile-device test. Test rotation and gestures only when the tool can perform
them. Mark unavailable hardware or input modes as blocked, with the exact
reason.

Do not create real irreversible side effects. Do not use a live account to test
payment, send messages, or delete user data. Use the app's safe test path or
mark that action as blocked and explain the missing setup.

## Critical visual review

Look at every screenshot from every device pass. For each screen, list what is
ugly, cramped, unclear, or misaligned. A screen with no problem gets no
entry. Do not skip a screen because the task worked.

Give each entry:

- The screen and the device it was seen on.
- A screenshot of the screen.
- The reason, in plain design terms: spacing, contrast, order, or alignment.

Use these words:

- **Spacing**: too tight, too loose, uneven gaps, text cut off, a control too
  close to the edge, a row that wraps in a strange place.
- **Contrast**: text that is hard to read, a border that cannot be seen, a
  state that looks the same as the normal state.
- **Order**: the wrong thing first, a title below its content, an action far
  from what it acts on, an important item at the bottom.
- **Alignment**: things that should line up and do not, a label not next to
  its field, a button off the grid, a ragged edge.

Say what the user feels, not what you would build. "The save button is under
the fold on a phone, so the user cannot see it" is a finding. "Use a 24 pixel
gap" is not, unless the repo's design system says so.

Keep the visual review in the report and in the tickets. A finding with no
screenshot is not ready.

## Design-system conformance

Find the design system and mockup files in the repo first. Read the repo
instructions and look for a design file, a token file, a component library, or
page. In Lupin, the mockups are `docs/ui.dc.html` and
`docs/ui-mockups.md`. If the repo has no design system, say so in the report
and check any mockup files that exist.

Compare each screen with those files. Report:

- Colors, sizes, or spacing that are not in the token set.
- A component built by hand where a standard one exists.
- Missing states: focus, hover, disabled, error, empty, loading.
- Mockup elements the screen does not have.

Give each item the file and the rule it comes from. Name the token, the
component, or the mockup section. "The card border uses `#e3e3e3`, which is
not in the token set in `docs/ui.dc.html`" is a finding. "The border color
looks off" is not.

Do not invent a style. Do not propose a new color, size, font, or spacing
value that the design system does not already have. If the design system has
no answer, say that the design team must decide. Check the states on the
screen, not only the happy path. The `Scenario` mockup in `docs/ui.dc.html`
shows the empty and failing states to check.

## Report findings and write tickets

The director merges agent reports, removes duplicates, and checks the evidence
before filing tickets. Keep a separate ticket for each independent problem or
request. Group findings only when they share the same cause and fix. Do not
turn a preference into a bug: state the user goal, what happened, and
why it causes friction. Include design-team requests when wording, layout,
information order, control visibility, or workflow complexity is the problem.
File the visual review and the design-system check as design requests. Each
one gets its screenshot and the file the rule comes from.

Use the `triage` skill and the repo's issue label map. Give every ticket a
`P0` to `P3` priority, mapped to the repo's labels when needed:

- `P0`: a core task is broken or very disruptive, or the app loses or exposes
  important user data. Escalate at once. Stop a test if continuing could cause
  more harm.
- `P1`: a common or important task is blocked or badly impaired, with no
  reasonable workaround.
- `P2`: a task works but has a clear defect or repeated friction; a workaround
  exists, or the issue affects a smaller part of the task.
- `P3`: a small or rare problem with low user impact, including polish that
  does not block the task.

Do not call a finding `P0` only because it is a bug. Explain the user impact
that supports the priority. If the repo has different labels, use its mapping
and preserve the matching level of urgency.

Do not put `ui-pressure-test` on every finding; it marks the QA task, not the
bugs it finds. Use the repo's issue-type and capability labels for each
finding. Use `frontend-ui` for visual UI findings and `cad-spatial` for CAD or
geometry findings. Split or route separate work lanes when both categories
need benchmark coverage.

Each ticket must include:

- A short title and type: bug, accessibility issue, feature request, or design
  request.
- Priority, Lupin capability (`frontend-ui` or `cad-spatial`), and affected
  journey, device, browser, and input method.
- User impact and the expected result.
- Exact steps to repeat the issue and what happened instead.
- Evidence links or attachments. When the pressure test covers a pull
  request, each device image is posted as a pull request comment. See "Post
  the images on the pull request". Say if the problem happened once or more
  than once.
- A clear recommendation. For a design request, say what users need to
  understand or do; do not prescribe a visual style without evidence.
- Acceptance checks that a designer or developer can verify.

File the tickets in the project's issue tracker when access and project rules
allow. If they do not, provide complete issue-ready ticket text and state why
the tickets were not filed. Use the repo's priority and issue-type labels; do
not invent labels or change the repo's label setup.

## Post the images on the pull request

When the pressure test covers a pull request, post each device image as a pull
request comment. Do this the way the `ship` skill does in "Attach screenshots
to the PR". Do not commit the images to the repo.

1. Get the pull request number and the repo that holds it. Use the pull
   request URL, not the fork name, unless the pull request is on the fork.
2. Write the comment body in a file, such as
   `evidence/<pull-request-number>/report.md`. Name each image in the body
   with the same path that goes into `--attach`.
3. Attach the images and post the comment:

   ```sh
   PR_NUMBER='<pull-request-number>'
   REPO='<owner>/<repo>'
   EVIDENCE_DIR="./evidence/$PR_NUMBER"
   gh pr comment "$PR_NUMBER" --repo "$REPO" \
     --attach "$EVIDENCE_DIR/phone-overview.png#phone overview" \
     --attach "$EVIDENCE_DIR/tablet-overview.png#tablet overview" \
     --attach "$EVIDENCE_DIR/desktop-overview.png#desktop overview" \
     --body-file "$EVIDENCE_DIR/report.md"
   ```

   Text after `#` is the image's alt text. One command takes up to 50 files.
4. Run `gh pr view "$PR_NUMBER" --repo "$REPO" --comments`. Each image
   must be a `https://github.com/user-attachments/...` link. A local path
   means the upload did not happen. Fix it and post again.

Post one comment per device pass when a journey has many screens, so each
comment is easy to read. Say what the reader should look at in each image. If
the upload fails, keep the image files, say in the report that the upload
failed, and give the exact error.

The coverage report links each pull request comment.

## Finish with an honest coverage report

Give the director's report these sections:

1. **Result** — the build and environment, test window, and high-level outcome.
2. **Coverage** — the journey and variation table, with one row per journey and
   device, with `passed`, `finding`, `blocked`, or `not run` status and the
   evidence location. A device that did not run is `not run`, with the reason.
   Link each pull request comment.
3. **Tickets** — each ticket ID or link, title, priority, and whether it is a
   bug or design request. Include issue-ready text if it could not be filed.
4. **Visual review** — the screen-by-screen list of what is ugly, cramped,
   unclear, or misaligned, with a screenshot and the reason for each item.
5. **Design-system check** — each item that breaks the repo's design system or
   mockups, with the file and the rule it comes from.
6. **Not tested** — every planned item not run or blocked, the exact reason,
   its impact on confidence, and the next step needed to test it.
7. **Risks** — any P0 issue, data safety concern, or unresolved uncertainty.

Do not say that everything passed if any planned item was not tested. Do not
hide gaps behind phrases such as “all major flows.” Complete the work only
when every planned item has a result, each finding has usable evidence, and
each recommendation is filed or ready to file. Keep the coverage report with
the tickets so the team can see what the test did and did not cover.
