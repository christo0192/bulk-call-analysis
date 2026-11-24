import { useEffect, useReducer, useRef, useState } from 'preact/hooks';
import { settingsOpen } from '../../state/store';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import type { JobSnapshot, Notification } from '../../engine/types';
import { initialJobLive, reduce } from '../../state/jobLive';

const number = (value: number) => new Intl.NumberFormat().format(value);
const duration = (seconds: number) => {
  const minutes = Math.max(0, Math.round(seconds / 60));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
};
const time = (ts: number) =>
  new Intl.DateTimeFormat(undefined, {
    weekday: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(ts * 1000);
const clockTime = (ts: number) =>
  new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(ts * 1000);
const activityAgo = (ts: number | null, now: number) => {
  if (ts === null) return '—';
  const seconds = Math.max(0, Math.floor(now / 1000 - ts));
  return seconds >= 60 ? `${Math.floor(seconds / 60)} min ago` : `${seconds} s ago`;
};
type BannerKind =
  | 'quota'
  | 'credit'
  | 'spend_control'
  | 'auth'
  | 'model_unavailable'
  | 'error_storm'
  | 'network'
  | 'sleep'
  | 'power'
  | 'stalled'
  | 'failed'
  | null;
const pauseReasons = new Set([
  'quota',
  'user',
  'sleep',
  'credit',
  'auth',
  'model_unavailable',
  'error_storm',
  'network',
  'spend_control',
  'power',
]);

function bannerFor(snapshot: JobSnapshot, stalled: boolean): BannerKind {
  if (snapshot.state === 'failed' || snapshot.state === 'cancelled') return 'failed';
  if (stalled || snapshot.state === 'stalled') return 'stalled';
  if (snapshot.pauseReason === 'user') return null;
  return snapshot.pauseReason && pauseReasons.has(snapshot.pauseReason)
    ? (snapshot.pauseReason as BannerKind)
    : null;
}

function StateBanner({
  kind,
  snapshot,
  onResume,
  onAsk,
}: {
  kind: BannerKind;
  snapshot: JobSnapshot;
  onResume: () => Promise<void>;
  onAsk?: () => void;
}) {
  const [help, setHelp] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const resume = () => {
    if (pending) return;
    setPending(true);
    setError(null);
    void onResume()
      .catch((reason) => {
        const parsed = EngineError.from(reason);
        setError(
          parsed.reason === 'job_running'
            ? 'Still finishing the leads in progress. Try again in a moment.'
            : parsed.message,
        );
      })
      .finally(() => setPending(false));
  };
  if (!kind) return null;
  const reset =
    snapshot.resumeAt === null
      ? null
      : snapshot.resumeAt <= Date.now() / 1000
        ? 'Starting again…'
        : `${time(snapshot.resumeAt)} (in ${duration(snapshot.resumeAt - Date.now() / 1000)})`;
  const copy: Record<Exclude<BannerKind, null | 'user'>, [string, string]> = {
    quota: [
      'Paused: ChatGPT allowance',
      reset
        ? `The app stopped sending new leads so it never runs onto paid credits. It starts again on its own when the allowance resets: ${reset}. ${number(snapshot.progress.remaining)} leads are waiting, and nothing was lost.`
        : 'Press Resume when your allowance is back.',
    ],
    credit: [
      'Paused to avoid paid credits',
      'ChatGPT reported a credit charge. Every lead in progress was stopped at once and put back in the queue (no tries used up). Check that automatic top-up is off in ChatGPT, then resume.',
    ],
    spend_control: [
      'Paused: spending control',
      'The app paused because its spending control was reached.',
    ],
    auth: ['Paused: sign-in needed', 'The app needs you to sign in again before it can continue.'],
    model_unavailable: [
      'Paused: model unavailable',
      'The selected model is unavailable. Choose another model in settings, then resume.',
    ],
    error_storm: [
      'Paused: too many errors',
      'The app paused after repeated reader errors. Check the run and resume when ready.',
    ],
    network: [
      'Paused: network unavailable',
      'The app paused because the network is unavailable. Resume when the connection is back.',
    ],
    sleep: [
      'Paused while the computer slept',
      'Paused while the computer slept. It starts again on its own.',
    ],
    power: [
      'Paused: computer is on battery',
      'The app paused to protect the run. It starts again when you plug in.',
    ],
    stalled: [
      'Stopped: needs attention',
      snapshot.stall?.text ?? 'The job stopped and needs attention.',
    ],
    failed: [
      snapshot.state === 'cancelled' ? 'Run cancelled' : 'Run failed',
      snapshot.failure?.text ?? 'The run failed.',
    ],
  };
  const [title, text] = copy[kind];
  return (
    <section class={`run-banner ${kind}`} role="status">
      <strong>{title}</strong>
      <p>{text}</p>
      {kind === 'credit' && (
        <div class="banner-actions">
          <button type="button" disabled={pending} onClick={resume}>
            Resume
          </button>
          <button type="button" onClick={() => setHelp(!help)}>
            How to switch it off
          </button>
        </div>
      )}
      {help && <p>Turn off automatic top-up in your ChatGPT settings before resuming.</p>}
      {['auth', 'model_unavailable', 'error_storm', 'network', 'spend_control'].includes(kind) && (
        <div class="banner-actions">
          <button
            type="button"
            onClick={kind === 'auth' ? () => (settingsOpen.value = true) : resume}
          >
            {kind === 'auth' ? 'Open settings' : 'Resume'}
          </button>
        </div>
      )}
      {kind === 'stalled' && (
        <div class="banner-actions">
          <button type="button" onClick={onAsk}>
            Ask Claude what to do
          </button>
        </div>
      )}
      {error && (
        <p class="dialog-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

function AllowanceTile({ snapshot }: { snapshot: JobSnapshot }) {
  const primary = snapshot.quota.codex?.primary;
  const window = primary?.windowDurationMins === 300 ? primary : null;
  const used = window?.usedPercent;
  const stop = snapshot.quota.codex?.stopAtPercent;
  const projected = snapshot.quota.projectedEndPercent;
  const windowName =
    window?.windowDurationMins === 300
      ? '5-hour'
      : window?.windowDurationMins === 10080
        ? 'this week'
        : 'allowance';
  return (
    <section class="run-tile">
      <div class="tile-title">ChatGPT allowance (reading)</div>
      {used === undefined ? (
        <div class="tile-value">Unknown</div>
      ) : (
        <>
          <div class="tile-value">
            {Math.round(used)}% used in the {windowName} window
          </div>
          <div class="allowance-meter">
            <span class="allowance-used" style={{ width: `${used}%` }} />
            {projected !== null && (
              <span
                class="allowance-projection"
                style={{ left: `${used}%`, width: `${Math.max(0, projected - used)}%` }}
              />
            )}
            {stop !== undefined && <i style={{ left: `${stop}%` }} />}
          </div>
          <div class="tile-labels">
            <span>{stop === undefined ? '' : `Pauses at ${Math.round(stop)}%`}</span>
            <span>Resets {window ? time(window.resetsAt) : 'unknown'}</span>
          </div>
          {projected !== null && (
            <div class="tile-sub">This job ends near {Math.round(projected)}%</div>
          )}
        </>
      )}
    </section>
  );
}

export function RunView({ id, onStalled }: { id: string; onStalled?: (text: string) => void }) {
  const [live, dispatch] = useReducer(reduce, id, initialJobLive);
  const liveStateRef = useRef<string | undefined>(undefined);
  liveStateRef.current = live.snapshot?.state;
  const [now, setNow] = useState(Date.now());
  const [reasonsOpen, setReasonsOpen] = useState(false);
  const sequence = useRef(0);
  const etaAt = useRef<number | null>(null);
  const etaSignature = useRef<string | null>(null);
  const loading = useRef(false);
  const reloadPending = useRef(false);
  const mounted = useRef(true);
  const activeJob = useRef(id);
  activeJob.current = id;
  const load = () => {
    if (!mounted.current) return;
    if (loading.current) {
      reloadPending.current = true;
      return;
    }
    loading.current = true;
    const jobId = id;
    const request = ++sequence.current;
    dispatch({ type: 'snapshot/requested', sequence: request });
    void bridge
      .call('job/snapshot', { job: jobId })
      .then((snapshot) => {
        if (mounted.current && activeJob.current === jobId && snapshot.job === jobId)
          dispatch({ type: 'snapshot/loaded', sequence: request, snapshot });
      })
      .catch((error: unknown) => {
        const e = EngineError.from(error);
        if (mounted.current && activeJob.current === jobId)
          dispatch({ type: 'snapshot/failed', sequence: request, error: e.message, code: e.code });
      })
      .finally(() => {
        if (activeJob.current !== jobId) return;
        loading.current = false;
        if (mounted.current && reloadPending.current) {
          reloadPending.current = false;
          load();
        }
      });
  };
  useEffect(() => {
    mounted.current = true;
    dispatch({ type: 'reset', job: id });
    load();
    const off = bridge.onNotification((notification: Notification) => {
      if (notification.params.job !== id) return;
      dispatch({ type: 'notification', notification });
      if (notification.method === 'job/state') load();
    });
    const tick = window.setInterval(() => {
      setNow(Date.now());
      dispatch({ type: 'tick', state: liveStateRef.current });
    }, 1000);
    const refresh = window.setInterval(load, 30000);
    return () => {
      mounted.current = false;
      loading.current = false;
      reloadPending.current = false;
      off();
      window.clearInterval(tick);
      window.clearInterval(refresh);
      sequence.current += 1;
    };
  }, [id]);
  const snapshot = live.snapshot;
  if (!snapshot)
    return (
      <div class="phase-view" data-testid="run-view">
        {live.errorCode === -32601
          ? 'Live progress isn’t available yet'
          : (live.error ?? 'Loading run…')}
      </div>
    );
  const { progress, counts } = snapshot;
  const unusable =
    counts.noTranscript + counts.transcriptionFailed + counts.tooShort + counts.notFound;
  const accounted =
    progress.done +
    progress.inFlight +
    progress.retrying +
    progress.failed +
    progress.heldBack +
    progress.remaining;
  const addsUp = accounted === progress.total;
  const etaKnown = snapshot.eta?.known && snapshot.eta.low !== null && snapshot.eta.high !== null;
  const nextEtaSignature = etaKnown ? `${snapshot.eta!.low}:${snapshot.eta!.high}` : null;
  if (nextEtaSignature !== etaSignature.current) {
    etaSignature.current = nextEtaSignature;
    etaAt.current = etaKnown ? Date.now() / 1000 : null;
  }
  const finishClock =
    etaKnown && etaAt.current !== null ? clockTime(etaAt.current + snapshot.eta!.high!) : null;
  const banner = bannerFor(snapshot, live.stalled);
  const resume = () => bridge.call('job/resume', { job: id }).then(() => undefined);
  const cells: [string, number, string][] = [
    ['Read', progress.done, 'finished'],
    ['Reading now', progress.inFlight, 'in progress'],
    ['Retrying', progress.retrying, 'retrying'],
    ['Left', progress.remaining, 'waiting'],
    ['Failed', progress.failed, 'after 2 tries'],
    ['Held back', progress.heldBack, 'not started'],
  ];
  const accounting = [
    [progress.done, 'read'],
    [progress.inFlight, 'in progress'],
    [progress.retrying, 'retrying'],
    [progress.failed, 'failed'],
    [progress.heldBack, 'held back'],
    [progress.remaining, 'left'],
  ]
    .filter(([value]) => value !== 0)
    .map(([value, label]) => `${number(value as number)} ${label}`)
    .join(' + ');
  const usability = [
    [counts.usable, 'usable'],
    [counts.noTranscript, 'no transcript'],
    [counts.transcriptionFailed, 'transcription failed'],
    [counts.tooShort, 'too short'],
    [counts.notFound, 'not found'],
  ]
    .filter(([value]) => value !== 0)
    .map(([value, label]) => `${number(value as number)} ${label}`)
    .join(' + ');
  const sample = progress.phase === 'sample';
  return (
    <div class="phase-view run-view" data-testid="run-view">
      <StateBanner
        kind={banner}
        snapshot={snapshot}
        onResume={resume}
        onAsk={() => onStalled?.(snapshot.stall?.text ?? 'The job stopped and needs attention.')}
      />
      {live.error && (
        <div class="view-error" role="alert">
          {live.errorCode === -32601
            ? 'Live progress isn’t available yet'
            : "Couldn't refresh. Retry"}
          <button type="button" onClick={load}>
            Retry
          </button>
        </div>
      )}
      <section class="run-hero">
        <div class="run-big-number">
          <strong>{number(progress.done)}</strong>
          <span>
            of {number(progress.total)} {sample ? 'sample leads read' : 'leads read'}
          </span>
          <b>{progress.total ? Math.round((progress.done / progress.total) * 100) : 0}%</b>
        </div>
        <div class="segmented-progress">
          <i
            class="done"
            style={{ width: `${progress.total ? (progress.done / progress.total) * 100 : 0}%` }}
          />
          <i
            class="inflight"
            style={{ width: `${progress.total ? (progress.inFlight / progress.total) * 100 : 0}%` }}
          />
        </div>
        <div class="run-hero-row">
          <div>
            <strong>
              {etaKnown
                ? `About ${duration(snapshot.eta!.high!)} left`
                : 'Working out the time left…'}
            </strong>
            {etaKnown && (
              <span>
                Somewhere between {duration(snapshot.eta!.low!)} and {duration(snapshot.eta!.high!)}{' '}
                · finishes around {finishClock}
              </span>
            )}
          </div>
          <div>
            <strong>Last activity {activityAgo(snapshot.activity.lastActivityTs, now)}</strong>
            <span>
              {snapshot.activity.workers} readers working ·{' '}
              {snapshot.activity.throughputPerMin === null
                ? '—'
                : snapshot.activity.throughputPerMin.toFixed(1)}{' '}
              leads a minute over the last 30 min
            </span>
          </div>
        </div>
        <p class="run-note">
          The app reads masked transcripts, keeps every lead accounted for, and never uses paid
          credits.
        </p>
      </section>
      <div class="run-tiles">
        <AllowanceTile snapshot={snapshot} />
        <section class="run-tile">
          <div class="tile-title">Claude allowance (chat)</div>
          <div class="tile-value">
            {snapshot.quota.claude &&
            Array.isArray(snapshot.quota.claude.windows) &&
            typeof snapshot.quota.claude.windows[0]?.usedPercent === 'number'
              ? `${Math.round(snapshot.quota.claude.windows[0].usedPercent as number)}% used`
              : 'Unknown'}
          </div>
        </section>
      </div>
      <section class="counts-panel">
        <h3>Counts</h3>
        <div class="counts-grid">
          {cells
            .filter(([, value]) => value !== 0)
            .map(([label, value, detail]) => (
              <div key={label}>
                <div class="count-label">{label}</div>
                <strong>{number(value)}</strong>
                <span>
                  {detail}
                  {label === 'Failed' && (
                    <button
                      type="button"
                      class="text-button"
                      onClick={() => setReasonsOpen(!reasonsOpen)}
                    >
                      See why
                    </button>
                  )}
                </span>
              </div>
            ))}
        </div>
        <div class="accounted">
          <span>
            {number(progress.total)} = {accounting}
          </span>
          {!addsUp && (
            <strong class="accounting-error">Doesn&apos;t add up — see the verify report</strong>
          )}
        </div>
        {unusable > 0 && (
          <div class="accounted">
            {number(counts.selected)} selected = {usability}
          </div>
        )}
        {reasonsOpen && (
          <div class="failure-reasons">
            {Object.entries(counts.failedByReason).map(([reason, value]) => (
              <div key={reason}>
                {reason}: {number(value)}
              </div>
            ))}
          </div>
        )}
      </section>
      <div class="run-lower">
        <section class="run-panel">
          <h3>Steps</h3>
          {snapshot.steps.map((step) => (
            <div class="step-row" key={step.id}>
              <span class={`step-icon ${step.status}`}>
                {step.status === 'done'
                  ? '✓'
                  : step.status === 'active'
                    ? '◌'
                    : step.status === 'waiting_for_you'
                      ? '◉'
                      : ''}
              </span>
              <span>
                {step.status === 'waiting_for_you'
                  ? 'Waiting for you'
                  : step.label.replace(/\s*\([^)]*n\/N[^)]*\)/gi, '')}
                {step.detail ? ` · ${step.detail}` : ''}
              </span>
            </div>
          ))}
        </section>
        <section class="run-panel">
          <h3>Recent activity</h3>
          {snapshot.recent
            .slice()
            .reverse()
            .map((item) => (
              <div class="recent-row" key={`${item.ts}-${item.text}`}>
                <time>{time(item.ts)}</time>
                <span>{item.text}</span>
              </div>
            ))}
        </section>
      </div>
    </div>
  );
}
