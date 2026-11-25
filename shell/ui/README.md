# UI

Run `npm install` and `npm run dev` in this directory. The dev server and the tests automatically use the in-memory mock bridge when Tauri internals are absent; `VITE_BRIDGE=mock npm run dev` forces it even in a Tauri dev window. The mock records calls, supports scripted responses, and emits JSON-RPC notifications for tests. In a Tauri window, the bridge delegates calls to `engine_call` and listens to `engine-event` and `engine/status`. The production build (`npm run build`) never contains the mock, and the build fails if it does.

See [`docs/ui.md`](../../docs/ui.md) for the mock switches, the UI rules and the screen test.
