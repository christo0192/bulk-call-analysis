import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks';
import type { JobSnapshot } from '../../engine/types';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';

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
const ago = (ts: number | null, now: number) =>
  ts === null ? '—' : `${Math.max(0, Math.floor(now / 1000 - ts))} s ago`;
type BannerKind = 'quota' | 'sleep' | 'battery' | 'stalled' | 'credits' | 'chat-off' | null;

function bannerFor(
  snapshot: JobSnapshot,
  event: Record<string, unknown> | null,
  onBattery: boolean,
): BannerKind {
  const reason = snapshot.pauseReason ?? String(event?.reason ?? '');
  if (reason.includes('credit') || reason.includes('spend')) return 'credits';
  if (snapshot.state === 'chat_off' || snapshot.state === 'chat-off') return 'chat-off';
  if (snapshot.resumeAt !== null || reason.includes('usage')) return 'quota';
  if (snapshot.state === 'stalled' || event?.kind === 'stall' || event?.kind === 'stalled')
    return 'stalled';
  if (reason.includes('battery') || onBattery) return 'battery';
  if (reason.includes('chat') || reason.includes('extra')) return 'chat-off';
  if (snapshot.state === 'running' && snapshot.recent.some((item) => /sleep/i.test(item.text)))
    return 'sleep';
  return null;
}

function StateBanner({ kind, snapshot }: { kind: BannerKind; snapshot: JobSnapshot }) {
  if (!kind) return null;
  const copy = {
    quota: [
      'Paused: ChatGPT allowance at its safety stop',
      `The app stopped sending new leads so it never runs onto paid credits. It starts again when the allowance resets: ${snapshot.resumeAt ? time(snapshot.resumeAt) : 'when the allowance resets'}. Nothing was lost.`,
    ],
    sleep: [
      'The computer went to sleep, then resumed',
      "Sleep doesn't count as a stall. Work went back in the queue when the computer woke up, and nothing was lost.",
    ],
    battery: [
      'On battery',
      'Still reading. Below 20% the app stops starting new leads and pauses cleanly. Plug in to keep going; it carries on when power returns.',
    ],
    stalled: [
      'Reading has stalled',
      'No reader has reported progress recently. The app is checking the readers and will retry safely; nothing already saved was lost.',
    ],
    credits: [
      'Paused to avoid paid credits',
      "The app paused before using paid credits. I've checked, resume when you are ready. Nothing was lost.",
    ],
    'chat-off': [
      'Chat is off because Claude extra usage is on',
      'The run can continue safely. Turn extra usage off in Claude, then resume chat when you are ready. Nothing was lost.',
    ],
  } as const;
  const [title, text] = copy[kind];
  return (
    <section class={`run-banner ${kind}`} role="status">
      <strong>{title}</strong>
      <p>{text}</p>
      {kind === 'credits' && (
        <div class="banner-actions">
          <button type="button">I've checked, resume</button>
          <button type="button">How to check</button>
        </div>
      )}
    </section>
  );
}

function AllowanceTile({ snapshot }: { snapshot: JobSnapshot }) {
  const primary = snapshot.quota.codex?.primary;
  const used = primary?.usedPercent;
  const projected =
    snapshot.quota.projectedEndPercent ?? snapshot.quota.codex?.projectedEndPercent ?? null;
  const stop = snapshot.quota.codex?.stopAtPercent;
  return (
    <section class="run-tile">
      <div class="tile-title">ChatGPT allowance (reading)</div>
      {used === undefined ? (
        <div class="tile-value">Unknown</div>
      ) : (
        <>
          <div class="tile-value">{used}% used this week</div>
          <div class="allowance-meter">
            <span class="allowance-used" style={{ width: `${used}%` }} />
            <span
              class="allowance-projection"
              style={{ left: `${used}%`, width: `${Math.max(0, (projected ?? used) - used)}%` }}
            />
            {stop !== undefined && <i style={{ left: `${stop}%` }} />}
          </div>
          <div class="tile-labels">
            <span>This job ends near {projected ?? 'unknown'}%</span>
            <span>Pauses at {stop ?? 'unknown'}%</span>
          </div>
        </>
      )}
    </section>
  );
}

export function RunView({ id }: { id: string }) {
  const [snapshot, setSnapshot] = useState<JobSnapshot | null>(null);
  const snapshotRef = useRef<JobSnapshot | null>(null);
  const pendingProgress = useRef<Partial<JobSnapshot['progress']>>({});
  const pendingEngineEvents = useRef(new Map<string, Record<string, unknown>>());
  const [now, setNow] = useState(Date.now());
  const [reasonsOpen, setReasonsOpen] = useState(false);
  const [folderAvailable, setFolderAvailable] = useState(true);
  const [onBattery, setOnBattery] = useState(false);
  const [event, setEvent] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const applyEngineEvent = (current: JobSnapshot, next: Record<string, unknown>) => {
    const result = { ...current };
    if (next.kind === 'eta') result.eta = next as JobSnapshot['eta'];
    if (next.kind === 'activity')
      result.activity = {
        ...current.activity,
        ...(typeof next.last_activity_ts === 'number' || next.last_activity_ts === null
          ? { lastActivityTs: next.last_activity_ts as number | null }
          : {}),
        ...(typeof next.in_flight === 'number' ? { inFlight: next.in_flight } : {}),
        ...(typeof next.workers === 'number' ? { workers: next.workers } : {}),
        ...(typeof next.throughput_per_min === 'number'
          ? { throughputPerMin: next.throughput_per_min }
          : {}),
      };
    if (next.kind === 'pause') {
      result.pauseReason = typeof next.reason === 'string' ? next.reason : current.pauseReason;
      result.resumeAt = typeof next.resume_at === 'number' ? next.resume_at : current.resumeAt;
    }
    return result;
  };
  const loadSnapshot = async () => {
    try {
      const loaded = await bridge.call('job/snapshot', { job: id });
      let next = {
        ...loaded,
        progress: { ...loaded.progress, ...pendingProgress.current },
      } as JobSnapshot;
      pendingEngineEvents.current.forEach((pending) => {
        next = applyEngineEvent(next, pending);
      });
      snapshotRef.current = next;
      setSnapshot(next);
      setError(null);
    } catch (e) {
      setError(EngineError.from(e).message);
    }
  };
  useEffect(() => {
    void loadSnapshot();
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(tick);
  }, [id]);
  useLayoutEffect(() => {
    const off = bridge.onNotification((notification) => {
      const p = notification.params as Record<string, unknown>;
      if (p.job !== id) return;
      if (notification.method === 'job/state') void loadSnapshot();
      if (notification.method === 'job/progress') {
        pendingProgress.current = { ...pendingProgress.current, ...p };
        setSnapshot((current) => {
          if (!current) return current;
          const next = {
            ...current,
            progress: { ...current.progress, ...p } as JobSnapshot['progress'],
          };
          snapshotRef.current = next;
          return next;
        });
      }
      if (notification.method === 'engine/event') {
        const next = (p.event ?? p) as Record<string, unknown>;
        setEvent(next);
        if (next.kind === 'power' || next.kind === 'power_status') {
          if (next.on_ac === false) setOnBattery(true);
          if (next.on_ac === true) setOnBattery(false);
        }
        pendingEngineEvents.current.set(String(next.kind), next);
        setSnapshot((current) => {
          if (!current) return current;
          const result = applyEngineEvent(current, next);
          snapshotRef.current = result;
          return result;
        });
      }
    });
    return off;
  }, [id]);
  useEffect(() => {
    if (!snapshot || snapshot.state !== 'running') return;
    const timer = window.setInterval(() => void loadSnapshot(), 30000);
    return () => window.clearInterval(timer);
  }, [id, snapshot?.state]);
  if (error) return <div class="view-error">Unable to load this run: {error}</div>;
  if (!snapshot)
    return (
      <div class="phase-view" data-testid="run-view">
        Loading run…
      </div>
    );
  const { progress, counts } = snapshot;
  const percent = progress.total ? Math.round((progress.done / progress.total) * 100) : 0;
  const accounted =
    progress.done +
    progress.inFlight +
    progress.remaining +
    progress.failed +
    progress.heldBack +
    counts.noTranscript;
  const addsUp = accounted === counts.selected;
  const banner = bannerFor(snapshot, event, onBattery);
  const etaKnown = snapshot.eta?.known && snapshot.eta.low !== null && snapshot.eta.high !== null;
  const eta = etaKnown
    ? `Somewhere between ${duration(snapshot.eta!.low!)} and ${duration(snapshot.eta!.high!)} · likely done around ${time(Date.now() / 1000 + snapshot.eta!.high!)}`
    : 'Working out the time left…';
  const pauseOrResume = async () => {
    await bridge.call(snapshot.state === 'paused' ? 'job/resume' : 'job/pause', { job: id });
    await loadSnapshot();
  };
  const openFolder = async () => {
    try {
      await bridge.call('open_project_folder', { project: snapshot.project });
    } catch (e) {
      if (EngineError.from(e).code === -32601) setFolderAvailable(false);
      else setError(EngineError.from(e).message);
    }
  };
  const cells = [
    ['Read', progress.done, 'finished'],
    [
      'Reading now',
      progress.inFlight + progress.retrying,
      progress.retrying ? `${progress.retrying} retrying` : 'in progress',
    ],
    ['Left', progress.remaining, 'waiting'],
    ['Failed', progress.failed, 'after 2 tries'],
    ['Held back', progress.heldBack, 'not started'],
    ['No transcript', counts.noTranscript, 'not read'],
    ['Over size limit', counts.capped, 'not read'],
    ['Selected', counts.selected, 'from the plan'],
  ] as const;
  return (
    <div class="phase-view run-view" data-testid="run-view">
      <div class="run-actions">
        <button type="button" onClick={() => void pauseOrResume()}>
          {snapshot.state === 'paused' ? 'Resume' : 'Pause'}
        </button>
        <button type="button" disabled={!folderAvailable} onClick={() => void openFolder()}>
          Open folder{!folderAvailable && ' (available soon)'}
        </button>
      </div>
      <StateBanner kind={banner} snapshot={snapshot} />
      <section class="run-hero">
        <div class="run-big-number">
          <strong>{number(progress.done)}</strong>
          <span>of {number(progress.total)} leads read</span>
          <b>{percent}%</b>
        </div>
        <div class="segmented-progress">
          <i class="done" style={{ width: `${(progress.done / progress.total) * 100}%` }} />
          <i class="inflight" style={{ width: `${(progress.inFlight / progress.total) * 100}%` }} />
          <i class="failed" style={{ width: `${(progress.failed / progress.total) * 100}%` }} />
          <i class="held" style={{ width: `${(progress.heldBack / progress.total) * 100}%` }} />
        </div>
        <div class="run-hero-row">
          <div>
            <strong>
              About {etaKnown ? duration(snapshot.eta!.high!) : 'an unknown time'} left
            </strong>
            <span>{eta}</span>
          </div>
          <div>
            <strong>Last activity {ago(snapshot.activity.lastActivityTs, now)}</strong>
            <span>
              {snapshot.activity.workers} readers working ·{' '}
              {snapshot.activity.throughputPerMin ?? '—'} leads a minute over the last 30 min
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
          <div class="tile-value">Unknown</div>
          <div class="tile-sub">Chat allowance information is not available yet.</div>
        </section>
        <section class="run-tile">
          <div class="tile-title">This computer</div>
          <div class="tile-value">Unknown</div>
          <div class="tile-sub">
            Power information is not available yet. The app keeps the computer awake.
          </div>
        </section>
      </div>
      <section class="counts-panel">
        <h3>Counts</h3>
        <div class="counts-grid">
          {cells.map(([label, value, detail]) => (
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
                    {' '}
                    · see why
                  </button>
                )}
              </span>
            </div>
          ))}
        </div>
        {reasonsOpen && (
          <div class="failure-reasons">
            <strong>Failure reasons</strong>
            {Object.keys(counts.failedByReason).length ? (
              Object.entries(counts.failedByReason).map(([reason, value]) => (
                <div key={reason}>
                  {reason}: {number(value)}
                </div>
              ))
            ) : (
              <div>No failure reasons recorded.</div>
            )}
          </div>
        )}
        <div class={`accounted ${addsUp ? '' : 'bad'}`}>
          <b>Every lead accounted for:</b> {number(counts.selected)} = {number(progress.done)} read
          + {number(progress.inFlight)} in progress + {number(progress.remaining)} left +{' '}
          {number(progress.failed)} failed + {number(progress.heldBack)} held back +{' '}
          {number(counts.noTranscript)} no transcript{!addsUp && <strong> · doesn't add up</strong>}
        </div>
      </section>
      <div class="run-lower">
        <section class="run-panel">
          <h3>Steps</h3>
          {snapshot.steps.map((step) => (
            <div class="step-row" key={step.id}>
              <span class={`step-icon ${step.status}`}>
                {step.status === 'done' ? '✓' : step.status === 'active' ? '◌' : ''}
              </span>
              <span>{step.label}</span>
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
