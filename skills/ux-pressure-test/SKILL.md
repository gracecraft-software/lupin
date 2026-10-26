---
name: ux-pressure-test
description: >-
  Test a web app's accessibility, performance, and resilience through its real
  interface. Use this skill for WCAG checks, keyboard and touch access, slow
  pages, input delay, offline behavior, and UX pressure tests. Run every planned
  journey at phone, tablet, and desktop sizes. Use it with ui-pressure-test for
  broad journey and visual review.
compatibility: Requires an isolated test or preview app and browser automation.
---

# Pressure-test web UX

Use the real browser interface. Check what a person can see and do. Do not infer
results from source code or automated checks alone.

## Set safe test conditions

1. Read the repo rules. Find the preview app, test account, test data, supported
   browsers, and known user journeys.
2. Use an isolated preview or test account. Use synthetic data. Do not use
   production data or make real changes. Do not send real messages, place
   orders, or delete user data. Do not send unbounded traffic.
3. Record the build, URL, browser, data, and test date. Keep the same browser and
   test conditions for each device size.
4. Use browser actions and inspect the rendered page. Do not claim a check passed
   when the browser cannot perform it. Write `not run` or `unavailable` and say
   why.
5. Save the report and screenshots outside the repo. Do not commit them.

## Use the shared coverage and ticket rules

Follow `skills/ui-pressure-test/SKILL.md` for the repo QA register, coverage
rules, ticket rules, PR image comments, and final report. This keeps both skills
consistent.

Use the same QA register columns:

| Item | Devices | Last QA date | Commit tested | Result | Evidence | Changed since? |
|---|---|---|---|---|---|---|

Use one coverage row for each planned check, journey, device, and network mode.
Record the browser, viewport, input method, synthetic data, expected result,
status, and evidence. Use `not started`, `passed`, `finding`, `blocked`, or `not
run`. Never mark an untried check as passed. For an untested device or mode,
write `not run` and give the reason.

Use these viewport sizes for every journey:

- Phone: 390x844 CSS px.
- Tablet: 768x1024 CSS px.
- Desktop: 1440x900 CSS px.

## Accessibility

Test the main journey with a keyboard only. Use Tab, Shift+Tab, Enter, Space,
arrow keys, and Escape when the page supports them. Check the focus order, that
focus stays visible, and that no step traps focus. Save a screenshot of each
focus finding.

Check every control in the journey:

- Use the browser accessibility tree to check its role and accessible name.
- Check that each field has a name, label, instructions, and error text that a
  screen reader can read.
- Use a screen reader only when one is available. Name the screen reader and
  version. Keyboard checks alone are not a screen-reader test.
- Measure text contrast in the rendered state. WCAG 2.2 AA needs at least
  4.5:1 for normal text and 3:1 for large text. Large text is at least 24 CSS px
  regular or about 19 CSS px bold.
- For non-text contrast, use [WCAG 2.2 SC 1.4.11](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html).
  Check for 3:1 contrast against adjacent colors only when visual information
  is needed to identify a control or its state. Do not apply this rule to
  decorative parts that do not identify a control or state. Use rendered
  foreground and background colors. Compute the ratio with relative luminance.
  If an image, transparency, or gradient prevents a reliable value, say so; do
  not guess.
- Measure each touch target's width and height with its CSS box. WCAG 2.2
  [SC 2.5.8](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)
  needs at least 24 x 24 CSS px unless an exception applies: spacing, an
  equivalent control on the same page, a target within a sentence or block of
  text, size set by the user agent and not changed by the author, or essential
  presentation.
- For spacing, a 24 CSS px diameter circle centered on each small target's
  bounding box must not touch another target or another small target's circle.
  Record the exception and evidence. Do not assume an exception applies.
- Then apply any stricter repo rule, even if a WCAG exception applies. For
  issue #69's scope only, buttons, selects, and checkboxes on `/loops` and
  `/machines` at 390 x 844 need 44 x 44 CSS px. Record the rule used.
- Set the viewport to 320 CSS px wide. Check that text, controls, and the main
  journey reflow without page-level horizontal scrolling or clipped content.
- Turn on `prefers-reduced-motion: reduce`. Repeat the journey. Check that
  non-essential movement stops or becomes less intense and that no information
  is lost. Report motion you could not test.

A contrast tool may help. Record the foreground color, background color, ratio,
and threshold. A tool result does not replace a check of the actual page.

## Performance

Measure the same main journey at all three viewport sizes. Use a fresh page for
page-load measures. Record the browser, cache state, and any CPU or network
limits. Do not mix cold-cache and warm-cache results.

- **Page load:** Read the Navigation Timing entry. Report
  `loadEventEnd - startTime` in milliseconds. Also record
  `domContentLoadedEventEnd` and `responseStart` when available. These are load
  events, not proof that the page is ready for every task.
- **Time to interactive (TTI):** Use a browser tool that reports TTI, such as a
  Lighthouse run that includes that metric. Record its version and settings. If
  no tool reports TTI, write `unavailable`. Do not call DOMContentLoaded or page
  load TTI.
- **Input latency:** List each main user action in the journey. Use the browser
  Event Timing API to record the time from the input event to the next paint.
  Run each action five times and report the median in milliseconds. If the API
  does not support the action, time from the browser input to the visible result
  and label it `input-to-visible proxy`. Do not present the proxy as a direct
  input-latency measure.
- **Long tasks:** Before page load, observe `longtask` entries. Count tasks over
  50 ms and report the longest duration during page load and the journey. If the
  browser does not support this entry type, write `unavailable`.
- **Page weight:** Sum the navigation and resource `transferSize` values. Report
  known bytes and the number of entries with an unknown size. A zero size can
  mean a cache hit or blocked cross-origin timing; do not count it as zero bytes
  without proof.
- **Memory:** Record memory on a named page and visible state. Run 10 repeats of
  the same safe actions, then measure again on that same page and state. Use
  `measureUserAgentSpecificMemory()` when available. Otherwise use
  `performance.memory.usedJSHeapSize` and label it `JavaScript heap proxy`.
  This is not total browser or device memory. If you cannot keep or restore the
  same page and state, write `unavailable`; do not compare different pages or
  states. If neither memory API works, write `unavailable`.

Use the same action order and wait for the same visible result each time. Keep
one latency result per main action in the report. Do not average different
actions together.

## Resilience

Run the main journey once on a slow network and once offline at each viewport.
Use only the isolated preview and synthetic data.

- For slow network, use browser network emulation at 500 kbit/s down, 500 kbit/s
  up, and 400 ms latency. Record the profile and whether the journey completes.
- For offline, load the starting page, turn off the network in browser tools,
  then run the journey. Record the message, saved state, and recovery after the
  network returns. Do not claim offline support just because an old page stays
  visible.

Label these checks as emulated. If the browser cannot apply the profile, write
`blocked` and give the reason. Do not retry in production.

## Report results

Include these sections:

1. **Result** — build, URL, date, and summary.
2. **Coverage** — one row per check, journey, device, and network mode. Include
   status and evidence.
3. **Accessibility** — keyboard path, focus, names and labels, contrast,
   targets, 320 CSS px reflow, and reduced motion.
4. **Performance** — page load, TTI, one latency per main action, long tasks,
   page weight, and memory. Include units, method, and proxy or unavailable
   labels.
5. **Resilience** — slow-network and offline results, recovery, and limits.
6. **Tickets** — each ticket or issue-ready text, using the shared ticket rules.
7. **Not tested and risks** — each gap, reason, effect on confidence, next step,
   and any data-safety risk.

Do not say all checks passed when a check was not run or a measure was
unavailable. File each separate finding as its own ticket unless findings share
a cause and fix. Use the repo's issue labels and P0 to P3 rules. Include user
impact, steps, expected result, evidence, a recommendation, and verifiable
acceptance checks. Stop if continuing could cause harm.

Link an existing issue only when its page, control, and acceptance checks match
the finding. If scope or evidence does not match, leave it unassigned. Topic
alone is not enough.