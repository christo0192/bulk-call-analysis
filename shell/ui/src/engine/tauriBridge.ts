import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { EngineBridge, Unsubscribe } from './client';
import { EngineError } from './client';
import type { Method, ParamsOf, ResultOf, Notification } from './types';
export const tauriBridge: EngineBridge = {
  async call<M extends Method>(method: M, params: ParamsOf<M>): Promise<ResultOf<M>> {
    try {
      return await invoke<ResultOf<M>>('engine_call', { method, params });
    } catch (e) {
      throw EngineError.from(e);
    }
  },
  onNotification(cb) {
    let off: (() => void) | undefined;
    void listen<Notification>('engine-event', (e) => cb(e.payload)).then((fn) => {
      off = fn;
    });
    return () => off?.();
  },
  onStatus(cb) {
    let off: (() => void) | undefined;
    void listen<{ status: string; reason?: string }>('engine/status', (e) => cb(e.payload)).then(
      (fn) => {
        off = fn;
      },
    );
    return () => off?.();
  },
  status: () => invoke<string>('engine_status'),
};
