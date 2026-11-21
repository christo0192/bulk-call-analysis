import { useEffect, useState } from 'preact/hooks';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import type { AuthStatus, SetupItem } from '../../engine/types';
import { load, setupItems, setupRequired } from '../../state/store';
import { useSignIn } from '../hooks/useSignIn';

const groups: Array<{ title: string; names: string[] }> = [
  { title: 'App engine installed', names: ['uv_python', 'venv'] },
  { title: 'Masking tools ready', names: ['spacy'] },
  { title: 'BigQuery connector found', names: ['connector', 'key'] },
  {
    title: 'Chat and reader programs downloaded and verified',
    names: ['claude_copy', 'codex_copy'],
  },
  { title: 'Sign in to Claude', names: ['claude_login'] },
  { title: 'Sign in to ChatGPT', names: ['chatgpt_login'] },
  { title: 'Model training is off in both accounts', names: ['training_off'] },
  { title: 'No paid extras switched on', names: ['extra_usage_off'] },
];
const detail: Record<string, string> = {
  training_off:
    "Claude: Settings → Privacy → turn off 'Help improve Claude'. ChatGPT: Settings → Data controls → turn off 'Improve the model for everyone'.",
  extra_usage_off:
    'Claude: Settings → Usage → turn off extra usage. ChatGPT: Settings → turn off automatic credit top-up.',
};
function ready(item?: SetupItem): boolean {
  return item?.status === 'ok' || item?.status === 'manual_done';
}

export function SetupChecklist() {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const { waitingFor, signIn, checkAgain } = useSignIn(setAuth, (message) =>
    setError(message || null),
  );
  const items = setupItems.value;
  useEffect(() => {
    void bridge
      .call('auth/status', {})
      .then(setAuth)
      .catch((cause) => {
        setError(EngineError.from(cause).message);
      });
  }, []);
  const itemFor = (name: string) => items.find((item) => item.name === name);
  const allReady = items.length > 0 && items.every(ready);
  async function checks() {
    try {
      const result = await bridge.call('setup/runChecks', {});
      setupItems.value = result.items;
      setAuth(await bridge.call('auth/status', {}));
    } catch (cause) {
      setError(EngineError.from(cause).message);
    }
  }
  async function markManual(name: string) {
    try {
      const result = await bridge.call('setup/markManual', { item: name });
      setupItems.value = items.map((item) => (item.name === name ? result : item));
    } catch (cause) {
      setError(EngineError.from(cause).message);
    }
  }
  return (
    <main class="setup-page">
      <div class="setup-card">
        <h1>Set up this computer</h1>
        <p class="muted">About 5 minutes. You only do this once.</p>
        {error && <p class="error">{error}</p>}
        <div class="checklist">
          {groups.map((group) => {
            const groupItems = group.names.map(itemFor);
            const good = groupItems.every(ready);
            const failed = groupItems.find((item) => item?.status === 'failed');
            const login = group.names[0] === 'claude_login' || group.names[0] === 'chatgpt_login';
            const manual =
              group.names[0] === 'training_off' || group.names[0] === 'extra_usage_off';
            const name = group.names[0]!;
            const item = itemFor(name);
            return (
              <section class={`check-row ${failed ? 'failed' : ''}`} key={group.title}>
                <span
                  class={`check-icon ${good ? 'ok' : failed ? 'bad' : 'pending'}`}
                  aria-hidden="true"
                >
                  {good ? '✓' : failed ? '!' : '·'}
                </span>
                <div class="check-copy">
                  <strong>
                    {name === 'chatgpt_login' && ready(item) ? 'Signed in to ChatGPT' : group.title}
                  </strong>
                  <span class="muted">
                    {failed?.detail ??
                      (waitingFor === (name === 'claude_login' ? 'claude' : 'codex')
                        ? 'Waiting for you to finish signing in in your browser…'
                        : name === 'chatgpt_login' && ready(item)
                          ? `Plan: ${auth?.codex.planType ?? 'unknown'} · weekly allowance ${auth?.codex.usagePercent ?? 0}% used`
                          : manual && expanded !== name
                            ? 'Manual check required.'
                            : '')}
                  </span>
                  {manual && expanded === name && <p class="instructions">{detail[name]}</p>}
                </div>
                <div class="check-actions">
                  {failed && <button onClick={() => void checks()}>Check again</button>}
                  {login && !good && !failed && (
                    <button
                      disabled={waitingFor === (name === 'claude_login' ? 'claude' : 'codex')}
                      onClick={() => void signIn(name === 'claude_login' ? 'claude' : 'codex')}
                    >
                      {waitingFor === (name === 'claude_login' ? 'claude' : 'codex')
                        ? 'Waiting…'
                        : 'Sign in'}
                    </button>
                  )}
                  {login && waitingFor === (name === 'claude_login' ? 'claude' : 'codex') && (
                    <button
                      onClick={() => checkAgain(name === 'claude_login' ? 'claude' : 'codex')}
                    >
                      Check again
                    </button>
                  )}
                  {manual && !good && (
                    <button onClick={() => void markManual(name)}>I've checked</button>
                  )}
                  {manual && (
                    <button
                      class="link-button"
                      onClick={() => setExpanded(expanded === name ? null : name)}
                    >
                      {expanded === name ? 'Hide' : 'Show me where'}
                    </button>
                  )}
                </div>
              </section>
            );
          })}
        </div>
        <button
          class="primary continue"
          disabled={!allReady}
          onClick={() => {
            setupRequired.value = false;
            void load();
          }}
        >
          Continue
        </button>
      </div>
    </main>
  );
}
