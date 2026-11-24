import { useEffect, useState } from 'preact/hooks';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import { openProjectFile, openProjectFolder } from '../../engine/shellCommands';
import type { JobOutputs, PlanCard } from '../../engine/types';
import { RebuildDialog } from './RebuildDialog';

const count = (value: number) => new Intl.NumberFormat().format(value);
const localTime = (seconds: number) => new Date(seconds * 1000).toLocaleString();
const elapsed = (seconds: number | null, asleep: number | null) => {
  const total = Math.max(0, Math.round((seconds ?? 0) / 60));
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  const main = hours ? `${hours} h ${minutes} m` : `${minutes} min`;
  return `${main}${asleep && asleep > 0 ? `, including ${Math.round(asleep / 60)} min asleep` : ''}`;
};
const fileError = (error: unknown) => {
  const parsed = EngineError.from(error);
  if (parsed.reason === 'file_not_found') return 'That file is no longer there';
  if (parsed.reason === 'type_not_allowed') return "The app can't open that file type";
  return parsed.message;
};

export function DoneView({
  id,
  project,
  rebuildOpen,
  onCloseRebuild,
}: {
  id: string;
  project: string;
  rebuildOpen: boolean;
  onCloseRebuild: () => void;
}) {
  const [outputs, setOutputs] = useState<JobOutputs | null>(null);
  const [plan, setPlan] = useState<PlanCard | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [openError, setOpenError] = useState<string | null>(null);
  const [shownIssues, setShownIssues] = useState(new Set<string>());
  useEffect(() => {
    void Promise.all([
      bridge.call('job/outputs', { job: id }),
      bridge.call('job/plan', { job: id }),
    ])
      .then(([nextOutputs, nextPlan]) => {
        setOutputs(nextOutputs);
        setPlan(nextPlan);
      })
      .catch((error) => setLoadError(EngineError.from(error).message));
  }, [id]);
  const open = async (name: string) => {
    setOpenError(null);
    try {
      await openProjectFile(project, name);
    } catch (error) {
      setOpenError(fileError(error));
    }
  };
  if (loadError) return <div class="view-error">Unable to load results: {loadError}</div>;
  if (!outputs || !plan)
    return (
      <div class="phase-view" data-testid="done-view">
        Loading results…
      </div>
    );
  const results = outputs.files
    .filter((file) => file.kind === 'results' && file.name.endsWith('.xlsx'))
    .sort((a, b) => fileVersion(b.name) - fileVersion(a.name))[0];
  const summary = outputs.files.find((file) => file.name.endsWith('summary.md'));
  const verify = outputs.files.find((file) => file.kind === 'report');
  const reconciliation = outputs.reconciliation;
  const terms: Array<[string, number]> = [
    ['Read', reconciliation.read],
    ['No transcript', reconciliation.noTranscript],
    ['Transcription failed', reconciliation.transcriptionFailed],
    ['Too short', reconciliation.tooShort],
    ['Not found', reconciliation.notFound],
    ['Held back', reconciliation.heldBack],
    ['Failed', reconciliation.failed],
    ['Not started', reconciliation.notStarted],
  ];
  return (
    <div class="phase-view done-view" data-testid="done-view">
      <section class="done-hero">
        <div class="done-big-number">
          <strong>{count(reconciliation.read)}</strong>
          <span>of {count(reconciliation.selected)} leads read</span>
        </div>
        <p>
          finished {localTime(outputs.finishedAt)} ·{' '}
          {elapsed(outputs.durationSeconds, outputs.asleepSeconds)}
        </p>
        {outputs.headline && <p class="done-headline">{outputs.headline}</p>}
        <div class="done-files">
          {results && (
            <button class="primary" onClick={() => void open(results.name)}>
              Open results
            </button>
          )}
          {summary && <button onClick={() => void open(summary.name)}>Open summary</button>}
          {verify && <button onClick={() => void open(verify.name)}>Open verify report</button>}
          <button
            onClick={() =>
              void openProjectFolder(project).catch((error) => setOpenError(fileError(error)))
            }
          >
            Open folder
          </button>
        </div>
        {openError && (
          <p class="done-error" role="alert">
            {openError}
          </p>
        )}
      </section>
      <div class="done-columns">
        <section class="done-panel reconciliation">
          <h2>Every lead accounted for</h2>
          <div class="reconciliation-line total">
            <b>Leads selected</b>
            <b>{count(reconciliation.selected)}</b>
          </div>
          {terms
            .filter(([, value]) => value !== 0)
            .map(([label, value]) => (
              <div class="reconciliation-line" key={label}>
                <span>{label}</span>
                <span>{count(value)}</span>
              </div>
            ))}
          <p class={reconciliation.addsUp ? 'reconciliation-ok' : 'reconciliation-bad'}>
            {reconciliation.addsUp
              ? `✓ Adds up: ${count(reconciliation.selected)}, no lead counted twice`
              : "Doesn't add up — see the verify report"}
          </p>
        </section>
        <section class="done-panel">
          <h2>Worth a look</h2>
          {outputs.issues.length === 0 && <p>Nothing was flagged.</p>}
          {outputs.issues.map((issue) => {
            const expanded = shownIssues.has(issue.kind);
            return (
              <div class="issue" key={issue.kind}>
                <p>
                  <b>{count(issue.count)}</b> {issue.text}
                </p>
                {issue.leads.length > 0 && (
                  <button
                    onClick={() =>
                      setShownIssues((current) => {
                        const next = new Set(current);
                        if (expanded) next.delete(issue.kind);
                        else next.add(issue.kind);
                        return next;
                      })
                    }
                  >
                    {expanded ? 'Hide' : 'View'}
                  </button>
                )}
                {expanded && (
                  <div class="lead-list selectable">{issue.leads.slice(0, 50).join(', ')}</div>
                )}
              </div>
            );
          })}
        </section>
      </div>
      {rebuildOpen && <RebuildDialog job={id} plan={plan} onClose={onCloseRebuild} />}
    </div>
  );
}

function fileVersion(name: string) {
  const rebuild = /rebuild(\d+)/i.exec(name);
  const version = /\.v(\d+)/i.exec(name);
  return Number(`${version?.[1] ?? 0}.${rebuild?.[1] ?? 0}`);
}
