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
  - In a plain browser it uses the in-memory mock (`engine/mockBridge.ts`). The mock records
    calls, accepts scripted responses, and emits notifications the way the engine does.
- **Fixtures:** `mock/fixtures/engine/*.json` were recorded from the real engine's lifecycle
  (plan, sample, running, paused and quota-paused snapshots, outputs, allowance and
  notifications). They are synthetic: no real names, emails or companies. When the engine's
  contract changes, record them again from the engine tests rather than editing them by hand.

## Run it while developing

From `shell/ui`:

| Command | What it does |
| --- | --- |
| `npm install` | Installs the UI's packages (once). |
| `VITE_BRIDGE=mock npm run dev` | Serves the UI on http://127.0.0.1:1420 with the mock engine. |
| `npm run typecheck` | Type-checks the UI. |
| `npm test` | Runs the Vitest unit and view tests (happy-dom). |
| `npm run e2e` | Runs the Playwright browser tests (it starts the dev server itself). |
| `npm run build` | Builds `dist/` for the desktop shell. |

`UI_PORT` changes the dev server's port, for example `UI_PORT=1430`, so two working copies can
run their browser tests side by side. The desktop shell expects 1420.

Add one of these `?mock=` switches to the dev URL to see a state the default data doesn't show:

| Switch | Shows |
| --- | --- |
| `?mock=firstrun` | The first-run checklist. |
| `?mock=noauth` | Signed out of Claude or ChatGPT. |
| `?mock=empty` | No projects yet. |
| `?mock=<reason>-pause` | The run banner for one pause reason: `quota`, `credit`, `spend-control`, `auth`, `model-unavailable`, `error-storm`, `network`, `sleep` or `power`. For example `?mock=quota-pause`. |
| `?mock=stalled`, `?mock=failed`, `?mock=cancelled` | Those run banners. |

In the default data, each sidebar project sits in a different phase: setting up, sample ready,
reading, paused and done.

## UI rules

- **Text never clips.** Use no ellipsis, line clamps, fixed heights on text, or
  `overflow: hidden` that cuts words. Labels wrap instead. The screen test enforces this.
- **Real controls.** Use buttons for actions and links for navigation. Icon-only buttons get an
  `aria-label`. Status pills and step markers carry words, never colour alone.
- **Keyboard.**
  - Every control can be reached with Tab, in order: sidebar, then the main view, then the
    chat drawer.
  - Every control shows a focus ring and works with Enter or Space.
  - Dialogs trap focus, close on Escape and give focus back to their opener
    (`app/hooks/useModalFocus.ts`).
  - When a view changes, focus moves to its heading.
- **Screen readers.**
  - Run progress is announced at most every 10 seconds.
  - A finished chat reply is announced once ("Claude replied"), never token by token.
  - "Saved" in Settings is a status.
  - The run banners are statuses.
- **Reduced motion.** With `prefers-reduced-motion: reduce`, nothing animates. The spinner
  shows "…" instead.
- **No inline scripts.** The content security policy forbids them.
- **Synthetic data only**, in fixtures, tests and screenshots.
- **Plain words.** Show engine states and errors in the words the screens use (for example
  "reading every lead", not `full_running`).

## The screen test

`e2e/screens.spec.ts` opens every main screen, each dialog and every banner at 1280x720,
1366x768 and 1920x1080. On each one it fails if:

- any element with visible text is clipped, truncated or line-clamped;
- any text sits outside the window;
- the console shows an error.

The failure message names the element and its text.

Every run saves a full-page screenshot of each screen to
`shell/ui/test-results/screens/<size>-<screen>.png`. These files are not committed. Look
through them after a visual change: the test catches clipping, but not a layout that only
looks wrong.

Other browser tests:

- `e2e/a11y.spec.ts`:
  - a keyboard-only walk (create a project, approve a plan, open Settings);
  - dialog focus;
  - a reduced-motion check that nothing animates.
- `e2e/app.spec.ts`, `chat.spec.ts`, `run.spec.ts` and `done.spec.ts`: the main flows,
  phase by phase.
