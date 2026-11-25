import { useEffect, useRef, useState } from 'preact/hooks';
import type { RefObject } from 'preact';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import type { AllowanceStatus, AuthStatus, EngineStatus, Settings } from '../../engine/types';
import { settingsOpen, setupRequired } from '../../state/store';
import { useSignIn } from '../hooks/useSignIn';
import { useModalFocus } from '../hooks/useModalFocus';
import { formatPlanUsage } from '../formatUsage';
import { weeklyPercent } from '../allowance';

const initialSettings: Settings = {
  keyPath: '',
  keyLocated: false,
  claudeModel: '',
  codexModel: '',
  workerCount: 1,
  retentionDays: 30,
};
const modelNames = { claudeModel: 'Claude', codexModel: 'ChatGPT' } as const;

/** Asks before a model change: it affects every new job. */
function ModelChangeDialog({
  name,
  draft,
  opener,
  onDraft,
  onConfirm,
  onCancel,
}: {
  name: string;
  draft: string;
  opener: RefObject<HTMLButtonElement>;
  onDraft: (value: string) => void;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  useModalFocus(dialog, onCancel, opener);
  return (
    <div class="modal-backdrop">
      <div
        class="model-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="model-dialog-title"
        ref={dialog}
      >
        <h2 id="model-dialog-title">Change the {name} model</h2>
        <p>
          Changing a model affects every new job. Results from different models aren't directly
          comparable.
        </p>
        <label htmlFor="model-value">New model</label>
        <input id="model-value" value={draft} onInput={(e) => onDraft(e.currentTarget.value)} />
        <div class="dialog-actions">
          <button onClick={onCancel}>Cancel</button>
          <button class="primary" onClick={onConfirm}>
            Change model
          </button>
        </div>
      </div>
    </div>
  );
}

export function SettingsView() {
  const claudeChange = useRef<HTMLButtonElement>(null);
  const codexChange = useRef<HTMLButtonElement>(null);
  // Settings replaces the sidebar and the button that opened it; start from the page heading.
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => heading.current?.focus(), []);
  const [values, setValues] = useState<Settings>(initialSettings);
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [allowance, setAllowance] = useState<AllowanceStatus | null>(null);
  const [app, setApp] = useState<EngineStatus | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [workersError, setWorkersError] = useState<string | null>(null);
  const [retentionError, setRetentionError] = useState<string | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [appError, setAppError] = useState<string | null>(null);
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const loadSettings = () => {
    setLoading(true);
    setError(null);
    setLoadError(null);
    void Promise.allSettled([
      bridge.call('settings/get', {}),
      bridge.call('auth/status', {}),
      bridge.call('app/status', {}),
      bridge.call('allowance/status', {}),
    ])
      .then(([s, a, p, allowanceResult]) => {
        if (s.status === 'fulfilled') {
          setValues({ ...s.value, keyPath: s.value.keyPath ?? s.value.connectorPath ?? '' });
          setSettingsLoaded(true);
        } else {
          const message = EngineError.from(s.reason).message;
          setLoadError(`Couldn't load settings: ${message}`);
        }
        if (a.status === 'fulfilled') setAuth(a.value);
        else setAuthError(EngineError.from(a.reason).message);
        if (p.status === 'fulfilled') setApp(p.value);
        else setAppError(EngineError.from(p.reason).message);
        if (allowanceResult.status === 'fulfilled') setAllowance(allowanceResult.value);
      })
      .finally(() => setLoading(false));
  };
  useEffect(loadSettings, []);
  const {
    waitingFor,
    isChecking: signInChecking,
    signIn,
    checkAgain,
  } = useSignIn(setAuth, (message) => setError(message || null));
  async function save(changes: Partial<Settings>, label: string) {
    setError(null);
    setFieldError(null);
    setWorkersError(null);
    try {
      const next = await bridge.call('settings/set', changes);
      setValues({ ...next, keyPath: next.keyPath ?? next.connectorPath ?? '' });
      setSaved(label);
      window.setTimeout(() => setSaved(null), 1600);
    } catch (cause) {
      const e = EngineError.from(cause);
      const message = e.code === -32602 ? "That value isn't valid" : e.message;
      if (label === 'connector') setFieldError(message);
      else if (label === 'workers') setWorkersError(message);
      else if (label === 'retention') {
        setRetentionError(message);
        try {
          const current = await bridge.call('settings/get', {});
          setValues({ ...current, keyPath: current.keyPath ?? current.connectorPath ?? '' });
        } catch (refreshCause) {
          setRetentionError(EngineError.from(refreshCause).message);
        }
      } else setError(message);
    }
  }
  return (
    <main class="settings-page">
      <header class="settings-head">
        <div>
          <h1 ref={heading} tabIndex={-1}>
            Settings
          </h1>
          <p class="muted">Configure this computer and future jobs.</p>
        </div>
        <button onClick={() => (settingsOpen.value = false)}>Back</button>
      </header>
      <div class="settings-scroll">
        {loading && <p class="muted">Loading settings…</p>}
        {loadError && (
          <div class="error-card">
            <p>{loadError}</p>
            <button onClick={loadSettings}>Try again</button>
          </div>
        )}
        <section class="settings-card">
          <h2>Sign-in</h2>
          {authError && <p class="error">{authError}</p>}
          <p>
            Claude: {auth?.claude.loggedIn ? 'Signed in' : 'Not signed in'}{' '}
            <button
              disabled={auth?.claude.loggedIn || waitingFor === 'claude'}
              onClick={() => void signIn('claude')}
            >
              {waitingFor === 'claude' ? 'Waiting…' : 'Sign in'}
            </button>
            {waitingFor === 'claude' && (
              <button disabled={signInChecking} onClick={() => checkAgain('claude')}>
                {signInChecking ? 'Checking…' : 'Check again'}
              </button>
            )}
          </p>
          <p>
            ChatGPT:{' '}
            {auth?.codex.loggedIn
              ? `Signed in · ${formatPlanUsage(auth.codex.planType, weeklyPercent(allowance))}`
              : 'Not signed in'}{' '}
            <button
              disabled={auth?.codex.loggedIn || waitingFor === 'codex'}
              onClick={() => void signIn('codex')}
            >
              {waitingFor === 'codex' ? 'Waiting…' : 'Sign in'}
            </button>
            {waitingFor === 'codex' && (
              <button disabled={signInChecking} onClick={() => checkAgain('codex')}>
                {signInChecking ? 'Checking…' : 'Check again'}
              </button>
            )}
          </p>
        </section>
        <section class="settings-card">
          <h2>BigQuery</h2>
          <label htmlFor="connector-path">Service-account key file</label>
          <div class="field-row">
            <input
              id="connector-path"
              value={String(values.keyPath ?? values.connectorPath ?? '')}
              disabled={!settingsLoaded}
              onInput={(e) => setValues({ ...values, keyPath: e.currentTarget.value })}
            />
            <button
              disabled={!settingsLoaded}
              onClick={() => void save({ connectorPath: values.keyPath ?? '' }, 'connector')}
            >
              Save
            </button>
            {saved === 'connector' && (
              <span class="saved" role="status">
                Saved
              </span>
            )}
          </div>
          {fieldError && <p class="error">{fieldError}</p>}
          <p class="muted">
            Where your read-only BigQuery key is stored. The app never reads its contents into the
            window.
          </p>
          <p class="muted">{values.keyLocated ? 'Key found' : 'Key not found'}</p>
        </section>
        <section class="settings-card">
          <h2>Models</h2>
          {(['claudeModel', 'codexModel'] as const).map((key) => (
            <div class="setting-line" key={key}>
              <span>
                {modelNames[key]}
                <b>{String(values[key])}</b>
              </span>
              <button
                ref={key === 'claudeModel' ? claudeChange : codexChange}
                aria-label={`Change ${modelNames[key]} model`}
                disabled={!settingsLoaded}
                onClick={() => {
                  setModel(key);
                  setDraft(String(values[key]));
                }}
              >
                Change…
              </button>
            </div>
          ))}
          {model && (
            <ModelChangeDialog
              name={modelNames[model as keyof typeof modelNames]}
              draft={draft}
              opener={model === 'claudeModel' ? claudeChange : codexChange}
              onDraft={setDraft}
              onConfirm={() => {
                void save({ [model]: draft, confirmModelChange: true }, 'model');
                setModel(null);
              }}
              onCancel={() => setModel(null)}
            />
          )}
          {error && <p class="error">{error}</p>}
        </section>
        <section class="settings-card">
          <h2>Workers</h2>
          <label htmlFor="workers">Worker count</label>
          <select
            id="workers"
            value={String(values.workerCount)}
            disabled={!settingsLoaded}
            onChange={(e) => void save({ workerCount: Number(e.currentTarget.value) }, 'workers')}
          >
            {[1, 2, 3, 4, 5, 6, 7, 8].map((n) => (
              <option value={n} key={n}>
                {n}
              </option>
            ))}
          </select>
          <p class="muted">Applies to the next job you start or resume.</p>
          {workersError && <p class="error">{workersError}</p>}
          {saved === 'workers' && (
            <span class="saved" role="status">
              Saved
            </span>
          )}
        </section>
        <section class="settings-card">
          <h2>Retention</h2>
          <label htmlFor="retention">Keep masked transcripts and chats for N days</label>
          <input
            id="retention"
            type="number"
            min="1"
            max="365"
            value={String(values.retentionDays)}
            disabled={!settingsLoaded}
            onChange={(e) => {
              const n = Number(e.currentTarget.value);
              if (n >= 1 && n <= 365) void save({ retentionDays: n }, 'retention');
            }}
          />
          {retentionError && <p class="error">{retentionError}</p>}
        </section>
        <section class="settings-card">
          <h2>Setup</h2>
          <button
            onClick={() => {
              setupRequired.value = true;
              settingsOpen.value = false;
            }}
          >
            Check setup again
          </button>
        </section>
        <section class="settings-card">
          <h2>About</h2>
          {appError && <p class="error">{appError}</p>}
          <p>Engine version: {app?.engineVersion}</p>
          <p class="mono selectable">{app?.dataDir}</p>
          <button disabled>Open folder</button> <span class="muted">(available soon)</span>
        </section>
      </div>
    </main>
  );
}
