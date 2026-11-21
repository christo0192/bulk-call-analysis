# UI

Run `npm install` and `npm run dev` in this directory. The browser automatically uses the in-memory mock bridge when Tauri internals are absent; `VITE_BRIDGE=mock npm run dev` makes that choice explicit. The mock records calls, supports scripted responses, and emits JSON-RPC notifications for tests. In a Tauri window, the bridge delegates calls to `engine_call` and listens to `engine-event` and `engine/status`.
