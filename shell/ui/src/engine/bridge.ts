import type { EngineBridge } from './client';
import { mockBridge } from './mockBridge';
import { tauriBridge } from './tauriBridge';
export const bridge: EngineBridge =
  import.meta.env.VITE_BRIDGE === 'mock' || !('__TAURI_INTERNALS__' in window)
    ? mockBridge
    : tauriBridge;
export { mockBridge };
