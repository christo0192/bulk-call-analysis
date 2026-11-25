import { useEffect, useState } from 'preact/hooks';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import type { AuthStatus, SetupItem } from '../../engine/types';
import { load, setupItems, setupRequired } from '../../state/store';
import { useSignIn } from '../hooks/useSignIn';
import { formatPlanUsage } from '../formatUsage';

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
function ready(item: SetupItem | undefined, auth: AuthStatus | null, name?: string): boolean {
  if (item?.status === 'ok' || item?.status === 'manual_done') return true;
  if (name === 'claude_login') return auth?.claude.loggedIn === true;
  if (name === 'chatgpt_login') return auth?.codex.loggedIn === true;
  return false;
}

export function SetupChecklist() {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const {
    waitingFor,
    isChecking: checking,
    signIn,
    checkAgain,
  } = useSignIn(setAuth, (message) => setError(message || null));
  const items = setupItems.value;
  useEffect(() => void checkAgain('claude'), []);
  const itemFor = (name: string) => items.find((item) => item.name === name);
  const allReady = items.length > 0 && items.every((item) => ready(item, auth, item.name));
  async function markManual(name: string) {
    try {
      const result = await bridge.call('setup/markManual', { item: name });
      setupItems.value = items.map((item) =>
        item.name === result.item ? { ...item, status: result.status } : item,
      );
    } catch (cause) {
      setError(EngineError.from(cause).message);
    }
  }
  return (
    <main class="setup-page">
      <div class="setup-card">
        <h1>Set up this computer</h1>
        <p class="muted">About 5 minutes. You only do this once.</p>
        {checking && <p class="muted">Checking…</p>}
        {error && <p class="error">{error}</p>}
        <div class="checklist">
          {groups.map((group) => {
            const groupItems = group.names.map(itemFor);
            const good = groupItems.every((item) => ready(item, auth, item?.name));
            const login = group.names[0] === 'claude_login' || group.names[0] === 'chatgpt_login';
            const failed = good ? undefined : groupItems.find((item) => item?.status === 'failed');
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
                    {/* The icon is only a shape and a colour; say the state in words too. */}
                    <span class="sr-only">
                      {good ? 'Done' : failed ? 'Needs attention' : 'Not done yet'}:{' '}
                    </span>
                    {name === 'chatgpt_login' && ready(item, auth, name)
                      ? 'Signed in to ChatGPT'
                      : group.title}
                  </strong>
                  <span class="muted">
                    {(failed &&
                      (login && failed.detail === 'probe not supplied'
                        ? 'Not signed in yet'
                        : failed.detail)) ??
                      (waitingFor === (name === 'claude_login' ? 'claude' : 'codex')
                        ? 'Waiting for you to finish signing in in your browser…'
                        : name === 'chatgpt_login' && ready(item, auth, name)
                          ? formatPlanUsage(auth?.codex.planType, auth?.codex.usagePercent)
                          : manual && expanded !== name
                            ? 'Manual check required.'
                            : '')}
                  </span>
                  {manual && expanded === name && <p class="instructions">{detail[name]}</p>}
                </div>
                <div class="check-actions">
                  {!login && failed && (
                    <button onClick={() => void checkAgain('claude')}>Check again</button>
                  )}
                  {login && !good && (
                    <button
                      disabled={waitingFor === (name === 'claude_login' ? 'claude' : 'codex')}
                      onClick={() => void signIn(name === 'claude_login' ? 'claude' : 'codex')}
                    >
                      {waitingFor === (name === 'claude_login' ? 'claude' : 'codex')
                        ? 'Waiting…'
                        : 'Sign in'}
                    </button>
                  )}
                  {login && !good && (
                    <button
                      aria-label="Check again"
                      disabled={checking}
                      onClick={() => checkAgain(name === 'claude_login' ? 'claude' : 'codex')}
                    >
                      {checking ? 'Checking…' : 'Check again'}
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
