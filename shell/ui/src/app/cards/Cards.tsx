import { useEffect, useState } from 'preact/hooks';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import type { AllowanceStatus, PlanCard as Plan, SampleCard as Sample } from '../../engine/types';

const number = (value: number) => value.toLocaleString();
const bytes = (value: number) =>
  value < 1024 ** 2
    ? `${(value / 1024).toFixed(1)} KB`
    : value < 1024 ** 3
      ? `${(value / 1024 ** 2).toFixed(1)} MB`
      : `${(value / 1024 ** 3).toFixed(1)} GB`;
const duration = (seconds: number) => {
  const minutes = Math.round(seconds / 60);
  return minutes < 60
    ? `${minutes} min`
    : `${Math.floor(minutes / 60)} h${minutes % 60 ? ` ${minutes % 60} min` : ''}`;
};
function ErrorLine({ error }: { error: unknown }) {
  const e = EngineError.from(error);
  return (
    <p class="card-error">
      {e.reason === 'stale_spec_version'
        ? 'The plan changed. Check the new version.'
        : 'That request could not be completed. Try again.'}
    </p>
  );
}
export function PlanCard({ job, onFocus }: { job: string; onFocus?: () => void }) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [sql, setSql] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const load = () => bridge.call('job/plan', { job }).then(setPlan).catch(setError);
  useEffect(() => {
    void load();
  }, [job]);
  if (!plan)
    return <div class="approval-card">{error ? <ErrorLine error={error} /> : 'Loading plan…'}</div>;
  if (plan.approved)
    return (
      <div class="approval-collapsed">
        Plan v{plan.specVersion} approved{' '}
        {plan.approvedAt
          ? new Date(plan.approvedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
          : ''}
      </div>
    );
  const count = plan.counts;
  const estimate = plan.estimate;
  const approve = async () => {
    setPending(true);
    setError(null);
    try {
      await bridge.call('job/approvePlan', { job, expectedSpecVersion: plan.specVersion });
    } catch (e) {
      setError(e);
      if (EngineError.from(e).reason === 'stale_spec_version') {
        setNotice('The plan changed. Check the new version.');
        void load();
      }
    } finally {
      setPending(false);
    }
  };
  return (
    <article class="approval-card plan-card">
      <header>
        <div>
          <h2>Plan</h2>
          <span>version {plan.specVersion}</span>
        </div>
      </header>
      <p class="question">{plan.question}</p>
      <dl>
        <dt>Leads</dt>
        <dd>
          {plan.leadSource.kind === 'sql' ? (
            <>
              {'SQL query '}
              <button class="link-button" onClick={() => setSql((value) => !value)}>
                {sql ? 'Hide SQL' : 'View SQL'}
              </button>
              {sql && <pre>{plan.leadSource.sql}</pre>}
            </>
          ) : (
            `${number(plan.leadSource.idCount ?? 0)} pasted ids`
          )}
        </dd>
        <dt>Calls read</dt>
        <dd>
          {plan.callsScope.mode === 'all'
            ? 'All calls'
            : `${plan.callsScope.windowStart ?? ''} – ${plan.callsScope.windowEnd ?? ''}`}
        </dd>
        <dt>Count</dt>
        <dd>
          {number(count.selected)} leads → {number(count.usable)} with a usable transcript ·{' '}
          {number(count.noTranscript)} with none
          {count.transcriptionFailed
            ? ` · ${number(count.transcriptionFailed)} transcription failed`
            : ''}
          {count.tooShort ? ` · ${number(count.tooShort)} too short` : ''}
          {count.notFound ? ` · ${number(count.notFound)} not found` : ''}
        </dd>
        <dt>Over the size limit</dt>
        <dd>{number(count.capped)}</dd>
        <dt>Data scanned</dt>
        <dd>{bytes(plan.scanBytes)}</dd>
        <dt>Output columns</dt>
        <dd class="chips">
          {plan.outputColumns.map((column) => (
            <code key={column.name}>{column.name}</code>
          ))}
        </dd>
        <dt>Rules</dt>
        <dd>
          {plan.rules.map((rule) => (
            <div key={`${rule.kind}-${rule.name}`}>{rule.summary}</div>
          ))}
        </dd>
      </dl>
      {estimate.sampleSeconds && (
        <p class="estimate">
          Sample of {number(plan.sampleSize)} leads: about {duration(estimate.sampleSeconds.low)} ·
          Full run:{' '}
          {estimate.fullSeconds
            ? `${duration(estimate.fullSeconds.low)}–${duration(estimate.fullSeconds.high)}`
            : 'unknown'}{' '}
          · ChatGPT weekly allowance:{' '}
          {estimate.codexPercent
            ? `about ${estimate.codexPercent.low}–${estimate.codexPercent.high}%`
            : 'unknown'}
        </p>
      )}
      {notice && <p class="card-notice">{notice}</p>}
      {error && <ErrorLine error={error} />}
      <footer>
        <button class="primary" disabled={pending} onClick={() => void approve()}>
          Approve plan
        </button>
        <button
          disabled={pending}
          onClick={() => {
            onFocus?.();
          }}
        >
          Change something
        </button>
        <small>
          Approving pulls and masks the transcripts, then reads a {number(plan.sampleSize)}-lead
          sample for you to check. The full run waits for you.
        </small>
      </footer>
    </article>
  );
}

function Masked({ value }: { value: unknown }) {
  const text = String(value ?? '');
  return (
    <>
      {text.split(/(\[[A-Z]+\])/g).map((part, index) =>
        /^\[[A-Z]+\]$/.test(part) ? (
          <code class="mask-chip" key={index}>
            {part}
          </code>
        ) : (
          part
        ),
      )}
    </>
  );
}
export function SampleCard({ job, onFocus }: { job: string; onFocus?: () => void }) {
  const [sample, setSample] = useState<Sample | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const load = () => bridge.call('job/sample', { job }).then(setSample).catch(setError);
  useEffect(() => {
    void load();
  }, [job]);
  if (!sample)
    return (
      <div class="approval-card">{error ? <ErrorLine error={error} /> : 'Loading sample…'}</div>
    );
  const columns = sample.columns.slice(0, 3);
  const approve = async () => {
    setPending(true);
    setError(null);
    try {
      await bridge.call('job/approveSample', { job, expectedSpecVersion: sample.specVersion });
    } catch (e) {
      setError(e);
    } finally {
      setPending(false);
    }
  };
  return (
    <article class="approval-card sample-card">
      <header>
        <div>
          <h2>Sample · {number(sample.total)} leads</h2>
          <span>
            quotes verified {number(sample.quotesVerified)} of {number(sample.total)} ·{' '}
            {number(sample.quotesRepaired)} fixed on retry
          </span>
        </div>
      </header>
      <div class="sample-table">
        <table>
          <thead>
            <tr>
              <th>Lead</th>
              {columns.map((column) => (
                <th key={column}>{column}</th>
              ))}
              <th>Quote (as the reader saw it)</th>
            </tr>
          </thead>
          <tbody>
            {sample.rows.slice(0, 5).map((row) => (
              <tr key={row.lead}>
                <td>{row.lead}</td>
                {columns.map((column) => (
                  <td key={column}>
                    <Masked value={row.answer[column]} />
                  </td>
                ))}
                <td>
                  <Masked value={row.quotes[0]?.text ?? '—'} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {error && <ErrorLine error={error} />}
      <footer>
        <button class="primary" disabled={pending} onClick={() => void approve()}>
          Approve sample
        </button>
        <button disabled>Open {sample.file ?? 'sample file'} (available soon)</button>
        <button onClick={() => onFocus?.()}>Change the plan</button>
        <small>{Math.max(0, sample.total - 5).toLocaleString()} more rows in the file</small>
      </footer>
    </article>
  );
}

export function StartCard({
  job,
  project,
  stopAtPercent = 95,
  allowance,
}: {
  job: string;
  project: string;
  stopAtPercent?: number;
  allowance?: AllowanceStatus | null;
}) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [cancelled, setCancelled] = useState(false);
  useEffect(() => {
    void bridge.call('job/plan', { job }).then(setPlan).catch(setError);
  }, [job]);
  const start = async () => {
    setPending(true);
    try {
      await bridge.call('job/startFull', { job });
    } catch (e) {
      setError(e);
    } finally {
      setPending(false);
    }
  };
  if (cancelled) return null;
  const window = allowance?.codex?.windows[0];
  const percent = window?.usedPercent;
  const estimate = plan?.estimate.fullSeconds;
  return (
    <article class="approval-card start-card">
      <header>
        <div>
          <h2>Start the full run?</h2>
          <span>
            sample approved{' '}
            {new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </span>
        </div>
      </header>
      <dl>
        <dt>Leads to read</dt>
        <dd>{number(plan?.counts.usable ?? 0)}</dd>
        <dt>Time</dt>
        <dd>
          {estimate
            ? `${duration(estimate.low)}–${duration(estimate.high)}`
            : "We'll know after a few minutes of reading"}
        </dd>
        <dt>ChatGPT allowance</dt>
        <dd>
          {percent === undefined
            ? 'Allowance projection is not available'
            : `${percent}% used now${plan?.estimate.codexPercent ? ` → about ${percent + plan.estimate.codexPercent.low}–${percent + plan.estimate.codexPercent.high}% after this job` : ''}${window ? ` · resets ${new Date(window.resetsAt).toLocaleString()}` : ''}`}
        </dd>
        <dt>Safety stop</dt>
        <dd>
          New leads stop at {stopAtPercent}% and the job waits for the reset. It never uses paid
          credits.
        </dd>
        <dt>This computer</dt>
        <dd>Keep it plugged in with the lid open. It's kept awake until the job ends.</dd>
      </dl>
      {error && <ErrorLine error={error} />}
      <footer>
        <button class="primary" disabled={pending} onClick={() => void start()}>
          Start full run
        </button>
        <button disabled={pending} onClick={() => setCancelled(true)}>
          Not now
        </button>
      </footer>
      <span class="sr-only">{project}</span>
    </article>
  );
}

export { Masked, bytes, number };
