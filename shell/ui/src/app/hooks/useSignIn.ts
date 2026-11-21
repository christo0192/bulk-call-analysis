import { useEffect, useState } from 'preact/hooks';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import type { AuthStatus, SetupItem } from '../../engine/types';
import { setupItems } from '../../state/store';

export type SignInVendor = 'claude' | 'codex';

export function useSignIn(onAuth: (auth: AuthStatus) => void, onError: (message: string) => void) {
  const [waitingFor, setWaitingFor] = useState<SignInVendor | null>(null);

  async function check(vendor: SignInVendor): Promise<void> {
    try {
      const [checks, auth] = await Promise.all([
        bridge.call('setup/runChecks', {}),
        bridge.call('auth/status', {}),
      ]);
      setupItems.value = checks.items;
      onAuth(auth);
      const name = vendor === 'claude' ? 'claude_login' : 'chatgpt_login';
      if (checks.items.some((item) => item.name === name && item.status === 'ok'))
        setWaitingFor(null);
    } catch (cause) {
      onError(EngineError.from(cause).message);
    }
  }

  useEffect(() => {
    if (!waitingFor) return;
    const vendor = waitingFor;
    const timer = window.setInterval(() => void check(vendor), 5000);
    const stop = window.setTimeout(() => setWaitingFor(null), 5 * 60 * 1000);
    return () => {
      window.clearInterval(timer);
      window.clearTimeout(stop);
    };
  }, [waitingFor]);

  async function signIn(vendor: SignInVendor): Promise<void> {
    onError('');
    setWaitingFor(vendor);
    try {
      await bridge.call(vendor === 'claude' ? 'auth/claudeLogin' : 'auth/codexLogin', {});
    } catch (cause) {
      setWaitingFor(null);
      onError(EngineError.from(cause).message);
    }
  }

  function checkAgain(vendor: SignInVendor): void {
    void check(vendor);
  }

  return { waitingFor, signIn, checkAgain };
}

export type SignInCheck = { items: SetupItem[]; auth: AuthStatus };
