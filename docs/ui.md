# UI and desktop run guide

To run the desktop app for real, follow the README's [Run it](../README.md#run-it)
section. It builds `shell/ui/dist` and starts the Tauri shell with the real stdio engine. It
uses neither the mock bridge nor a development server.

This guide covers working on the UI itself.

## How the UI is built

- **Stack:** Preact 10, TypeScript, Vite, and plain CSS (`shell/ui/src/styles`: `tokens.css`,
  `base.css`, `components.css`). There is no UI framework and no CSS-in-JS.
- **Folders** (in `shell/ui/src`):
  - `app/`: views, cards, chat and hooks. The phase views live in `app/views`: setup, sample,
    run, done, settings and the rebuild dialog.
  - `state/`: shared signals (`store.ts`) and the live job reducer (`jobLive.ts`).
  - `engine/`: the JSON-RPC client, the bridge, and the engine's types (`types.ts`).
  - `mock/`: the dev mock's data.
- **Bridge:** `engine/bridge.ts` chooses the transport.
  - Inside a Tauri window it calls `engine_call` and listens to `engine-event` and
    `engine/status`.
  - Without Tauri (the dev server, Vitest and Playwright) it loads the in-memory mock
    (`engine/mockBridge.ts`) with a dynamic import. `VITE_BRIDGE=mock` forces the mock inside a
    Tauri dev window too. The mock records calls, accepts scripted responses, and emits
    notifications the way the engine does. Tests import it from `engine/mockBridge`.
  - The production build never contains the mock, its `?mock=` switches or its fixtures: the
    import sits behind `import.meta.env.DEV`, which `vite build` sets to false. Opened outside
    the desktop app, a production build shows "The engine is unavailable."
- **Fixtures:** `mock/fixtures/engine/*.json` were recorded from the real engine's lifecycle
  (plan, sample, running, paused and quota-paused snapshots, outputs, allowance and
  notifications). They are synthetic: no real names, emails or companies. When the engine's
  contract changes, record them again from the engine tests rather than editing them by hand.

## Run it while developing

From `shell/ui`:

| Command                        | What it does                                                                                                                          |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| `npm install`                  | Installs the UI's packages (once).                                                                                                    |
| `VITE_BRIDGE=mock npm run dev` | Serves the UI on http://127.0.0.1:1420 with the mock engine.                                                                          |
| `npm run typecheck`            | Type-checks the UI.                                                                                                                   |
| `npm test`                     | Runs the Vitest unit and view tests (happy-dom).                                                                                      |
| `npm run e2e`                  | Runs the Playwright browser tests (it starts the dev server itself).                                                                  |
| `npm run build`                | Builds `dist/` for the desktop shell, then runs `npm run check:dist`.                                                                 |
| `npm run check:dist`           | Fails if any file in `dist/` contains `synthetic-job` or `mock=`, that is, if the mock got into the build (`scripts/check-dist.mjs`). |

`UI_PORT` changes the dev server's port, for example `UI_PORT=1430`, so two working copies can
run their browser tests side by side. The desktop shell expects 1420.

Add one of these `?mock=` switches to the dev URL to see a state the default data doesn't show:

| Switch                                             | Shows                                                                                                                                                                                |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `?mock=firstrun`                                   | The first-run checklist.                                                                                                                                                             |
| `?mock=noauth`                                     | Signed out of Claude and ChatGPT; the sign-in status check fails.                                                                                                                    |
| `?mock=empty`                                      | No projects yet.                                                                                                                                                                     |
| `?mock=<reason>-pause`                             | The run banner for one pause reason: `quota`, `credit`, `spend-control`, `auth`, `model-unavailable`, `error-storm`, `network`, `sleep` or `power`. For example `?mock=quota-pause`. |
| `?mock=stalled`, `?mock=failed`, `?mock=cancelled` | Those run banners.                                                                                                                                                                   |
| `?mock=engine-<status>`                            | The engine-status banner above the window: `starting`, `restarting`, `stopped` or `error`. For example `?mock=engine-restarting`. The projects still load.                           |

In the default data, each sidebar project sits in a different phase: setting up, sample ready,
reading, paused and done.

## UI rules

- **Text never clips.** Use no ellipsis, line clamps, fixed heights on text, or
  `overflow: hidden` that cuts words, on the text or on any box around it. Labels wrap instead.
  The screen test enforces this.
- **Readable lines on wide windows.** The app fills its window, so the run and done cards can be
  very wide. Their paragraphs stay at most 75 characters wide (`max-width: 75ch`), and a done
  view label stays within 30rem of its number. The screen test checks this at 2560x1440.
- **Real controls.** Use buttons for actions and links for navigation. Icon-only buttons get an
  `aria-label`. Status pills and step markers carry words, never colour alone.
- **Keyboard.**
  - Every control can be reached with Tab, in order: sidebar, then the main view, then the
    chat drawer.
  - Every control shows a focus ring and works with Enter or Space.
  - Dialogs trap focus, close on Escape and give focus back to their opener
    (`app/hooks/useModalFocus.ts`).
  - When a phase change removes the focused control, focus moves to the project heading. That
    includes the chat box when a finished run folds the chat drawer away.
  - Inside a dialog, a control that disappears or is disabled hands focus on, so it never drops
    to the page. In the rebuild dialog: Remove moves focus to the next rule's Remove button (or
    the one above, or the "Current rules" heading); Rebuild with no rules moves it to "Rebuild
    without rules"; "Keep editing" moves it back to Rebuild; a finished rebuild moves it to Open;
    a failed one back to the button that was pressed.
  - In the chat box, Enter sends and Shift+Enter adds a line. While an input method is
    composing (for example Japanese or Chinese), Enter is left to it and sends nothing.
- **Screen readers.**
  - Run progress is announced at most every 10 seconds, and then only the newest value.
  - A finished chat reply is announced once ("Claude replied"), never token by token.
  - "Saved" in Settings is a status.
  - "Rebuilt: <file>" in the rebuild dialog is a status. Its region is always in the page, so
    it is announced when the text appears.
  - The run banners' title and message are statuses. Anything that changes while a banner shows,
    such as the quota banner's "Starts again in 2 h 59 min", sits outside that status, so the
    banner is not read out again every minute.
- **Reduced motion.** With `prefers-reduced-motion: reduce`, nothing animates. The spinner
  shows "…" instead.
- **No inline scripts.** The content security policy forbids them.
- **Synthetic data only**, in fixtures, tests and screenshots.
- **Plain words.** Show engine states and errors in the words the screens use (for example
  "reading every lead", not `full_running`).

## The screen test

`e2e/screens.spec.ts` opens every main screen, each dialog and every banner (the run banners
and the engine-status banner) at 1280x720, 1366x768, 1920x1080 and 2560x1440. It first checks
that the intended view appeared (for example the "Approve plan" button or the Settings
heading). Then it fails if:

- any element with visible text is clipped, truncated or line-clamped, by its own overflow or
  squeezed into a box of 1px or less;
- any text is cut off by a parent with `overflow: hidden` or `clip` (its box reaches past the
  parent's), unless a scrolling box in between lets the person scroll to it;
- any text sits outside the window;
- the console shows an error.

Only text hidden on purpose for screen readers is skipped: inside `.sr-only`, or under a
`clip: rect(0 0 0 0)` or `clip-path: inset(50%)`. The failure message names the element and
its text.

Two more groups run with it:

- "the layout check" breaks the done view on purpose (a 60px-high panel with
  `overflow: hidden`, a 1px-wide heading) and expects the check to report both, so the check
  itself cannot quietly stop working;
- "a 2560x1440 window" checks the line length of the run and done cards (see the UI rules).

Every run saves a full-page screenshot of each screen to
`shell/ui/test-results/screens/<size>-<screen>.png`. These files are not committed. Look
through them after a visual change: the test catches clipping, but not a layout that only
looks wrong.

Other browser tests:

- `e2e/a11y.spec.ts`:
  - a keyboard-only walk (create a project, approve a plan, open Settings);
  - dialog focus, including where focus goes in the rebuild dialog after Remove and Rebuild;
  - focus moving to the heading when a run finishes while the chat box has it;
  - a reduced-motion check that nothing animates.
- `e2e/app.spec.ts`, `chat.spec.ts`, `run.spec.ts` and `done.spec.ts`: the main flows,
  phase by phase.
