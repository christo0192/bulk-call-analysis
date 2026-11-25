import { useEffect, useRef, useState } from 'preact/hooks';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import type { AuthStatus, SetupItem } from '../../engine/types';
import { setupItems } from '../../state/store';

export type SignInVendor = 'claude' | 'codex';

/** Plain words for a failed sign-in status check; the engine's own text stays in the log. */
export function signInCheckMessage(cause: unknown): string {
  const error = EngineError.from(cause);
  if (error.reason === 'app_server_unavailable' || /app-server did not start/i.test(error.message))
    return "Couldn't check the ChatGPT sign-in: the Codex program didn't start. Sign in to ChatGPT below, then press Check again.";
  if (error.code === -32601) return '';
  return `Couldn't check the sign-in status (${error.message}). Press Check again.`;
}

export function useSignIn(onAuth: (auth: AuthStatus) => void, onError: (message: string) => void) {
  const [waitingFor, setWaitingFor] = useState<SignInVendor | null>(null);
  const [isChecking, setIsChecking] = useState(false);
  const inFlight = useRef(false);
  const lastAuthCheck = useRef(0);
  const lastAuth = useRef<AuthStatus | null>(null);

  async function check(vendor: SignInVendor, explicit = false): Promise<void> {
    if (inFlight.current) return;
    inFlight.current = true;
    setIsChecking(true);
    try {
      const shouldCheckAuth =
        explicit || lastAuthCheck.current === 0 || Date.now() - lastAuthCheck.current >= 15000;
      if (shouldCheckAuth) lastAuthCheck.current = Date.now();
      const checks = await bridge.call('setup/runChecks', {});
      let auth = lastAuth.current ?? { claude: { loggedIn: false }, codex: { loggedIn: false } };
      if (shouldCheckAuth) {
        try {
          auth = await bridge.call('auth/status', {});
          lastAuth.current = auth;
          onError('');
        } catch (cause) {
          onError(signInCheckMessage(cause));
        }
      }
      setupItems.value = checks.items;
      onAuth(auth);
      const name = vendor === 'claude' ? 'claude_login' : 'chatgpt_login';
      if (
        checks.items.some((item) => item.name === name && item.status === 'ok') ||
        auth[vendor].loggedIn
      )
        setWaitingFor(null);
    } catch (cause) {
      onError(EngineError.from(cause).message);
    } finally {
      inFlight.current = false;
      setIsChecking(false);
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
    void check(vendor, true);
  }

  return { waitingFor, isChecking, signIn, checkAgain };
}

export type SignInCheck = { items: SetupItem[]; auth: AuthStatus };
