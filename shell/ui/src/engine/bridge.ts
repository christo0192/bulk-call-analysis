import type { EngineBridge, Unsubscribe } from './client';
import { EngineError } from './client';
import type { MockBridge } from './mockBridge';
import type { Method, ParamsOf, ResultOf } from './types';
import { tauriBridge } from './tauriBridge';

const inTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;

/**
 * The in-memory mock, for the dev server, Vitest and Playwright: wherever Tauri is absent
 * (`VITE_BRIDGE=mock` forces it inside a Tauri dev window too). `vite build` replaces
 * `import.meta.env.DEV` with false, so this import is dead code there: the production build
 * never contains the mock, its `?mock=` switches or its synthetic fixtures. `npm run build`
 * checks that (scripts/check-dist.mjs). Null wherever the mock is not used.
 */
export const mockBridgeLoaded: Promise<MockBridge> | null =
  import.meta.env.DEV && (!inTauri || import.meta.env.VITE_BRIDGE === 'mock')
    ? import('./mockBridge').then((module) => module.mockBridge)
    : null;

/** A production build opened outside the desktop app has no engine to talk to. */
const noEngine: EngineBridge = {
  call: async () => {
    throw new EngineError(null, 'The engine runs only inside the desktop app.');
  },
  onNotification: () => () => undefined,
  onStatus: () => () => undefined,
  status: async () => 'error',
};

/** Passes everything to the bridge once it has loaded; listeners added before then are kept. */
function whenLoaded(loading: Promise<EngineBridge>): EngineBridge {
  let loaded: EngineBridge | null = null;
  void loading.then((bridge) => {
    loaded = bridge;
  });
  const listen = (subscribe: (bridge: EngineBridge) => Unsubscribe): Unsubscribe => {
    if (loaded) return subscribe(loaded);
    let off: Unsubscribe | undefined;
    let removed = false;
    void loading.then((bridge) => {
      if (!removed) off = subscribe(bridge);
    });
    return () => {
      removed = true;
      off?.();
    };
  };
  // Once loaded, calls go straight through: no extra await, so the timing is the bridge's own.
  return {
    call<M extends Method>(method: M, params: ParamsOf<M>): Promise<ResultOf<M>> {
      return loaded
        ? loaded.call(method, params)
        : loading.then((bridge) => bridge.call(method, params));
    },
    onNotification: (cb) => listen((bridge) => bridge.onNotification(cb)),
    onStatus: (cb) => listen((bridge) => bridge.onStatus(cb)),
    status: () => (loaded ? loaded.status() : loading.then((bridge) => bridge.status())),
  };
}

/** Inside the desktop app this is the Tauri bridge itself, unchanged. */
export const bridge: EngineBridge = mockBridgeLoaded
  ? whenLoaded(mockBridgeLoaded)
  : inTauri
    ? tauriBridge
    : noEngine;
