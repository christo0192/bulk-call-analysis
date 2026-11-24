import { useEffect, useState } from 'preact/hooks';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import { openProjectFile, openProjectFolder } from '../../engine/shellCommands';
import type { JobOutputs, PlanCard, OutputFile } from '../../engine/types';
import { RebuildDialog } from './RebuildDialog';

const count = (value: number) => new Intl.NumberFormat().format(value);
const localTime = (seconds: number | null) =>
  seconds == null ? null : new Date(seconds * 1000).toLocaleString();
const elapsed = (seconds: number | null, asleep: number | null) => {
  if (seconds == null) return null;
  const total = Math.max(0, Math.round(seconds / 60));
  const main = total >= 60 ? `${Math.floor(total / 60)} h ${total % 60} m` : `${total} min`;
  return `${main}${asleep != null && asleep > 0 ? `, including ${Math.round(asleep / 60)} min asleep` : ''}`;
};
const fileError = (error: unknown) => {
  const parsed = EngineError.from(error);
  if (parsed.reason === 'file_not_found') return 'That file is no longer there';
  if (parsed.reason === 'type_not_allowed') return "The app can't open that file type";
  return parsed.message;
};
const fileRank = (file: OutputFile) => [file.specVersion ?? -1, file.rebuild ?? -1] as const;

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
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let current = true;
    setOutputs(null);
    setPlan(null);
    setLoadError(null);
    void bridge
      .call('job/outputs', { job: id })
      .then((value) => {
        if (current) setOutputs(value);
      })
      .catch((error) => {
        if (current) setLoadError(errorCode(error));
      });
    void bridge
      .call('job/plan', { job: id })
      .then((value) => {
        if (current) setPlan(value);
      })
      .catch(() => {
        /* results remain useful without a plan */
      });
    return () => {
      current = false;
    };
  }, [id, reload]);
  const open = async (path: string) => {
    setOpenError(null);
    try {
      await openProjectFile(project, path);
    } catch (error) {
      setOpenError(fileError(error));
    }
  };
  if (loadError) return <div class="view-error">Unable to load results: {loadError}</div>;
  if (!outputs)
    return (
      <div class="phase-view" data-testid="done-view">
        Loading results…
      </div>
    );
  const files = outputs.files;
  const results = files
    .filter((file) => file.kind === 'results' && file.name.endsWith('.xlsx'))
    .sort((a, b) => compareRank(fileRank(b), fileRank(a)))[0];
  const summary = files.find((file) => file.kind === 'summary');
  const verify =
    files.find((file) => file.kind === 'report' && file.specVersion === results?.specVersion) ??
    files.find((file) => file.kind === 'report');
  const reconciliation = outputs.reconciliation;
  const terms = reconciliation
    ? ([
        ['Read', reconciliation.read],
        ['No transcript', reconciliation.noTranscript],
        ['Transcription failed', reconciliation.transcriptionFailed],
        ['Too short', reconciliation.tooShort],
        ['Not found', reconciliation.notFound],
        ['Held back', reconciliation.heldBack],
        ['Failed', reconciliation.failed],
        ['Not started', reconciliation.notStarted],
      ] as Array<[string, number]>)
    : [];
  return (
    <div class="phase-view done-view" data-testid="done-view">
      <section class="done-hero">
        {reconciliation ? (
          <div class="done-big-number">
            <strong>{count(reconciliation.read)}</strong>
            <span>of {count(reconciliation.selected)} leads read</span>
          </div>
        ) : (
          <p>The reconciliation isn't available</p>
        )}
        {(outputs.finishedAt != null || outputs.durationSeconds != null) && (
          <p>
            {outputs.finishedAt != null && <>finished {localTime(outputs.finishedAt)}</>}
            {outputs.finishedAt != null && outputs.durationSeconds != null && ' · '}
            {elapsed(outputs.durationSeconds, outputs.asleepSeconds)}
          </p>
        )}
        {outputs.headline && <p class="done-headline">{outputs.headline}</p>}
        <div class="done-files">
          {results && (
            <button class="primary" onClick={() => void open(results.path)}>
              Open results
            </button>
          )}
          {summary && <button onClick={() => void open(summary.path)}>Open summary</button>}
          {verify && <button onClick={() => void open(verify.path)}>Open verify report</button>}
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
      {reconciliation && (
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
              const listed = issue.leads.slice(0, 50);
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
                          expanded ? next.delete(issue.kind) : next.add(issue.kind);
                          return next;
                        })
                      }
                    >
                      {expanded ? 'Hide' : 'View'}
                    </button>
                  )}
                  {expanded && (
                    <>
                      <p>
                        First{' '}
                        {listed.length < issue.leads.length
                          ? `50 of ${count(issue.count)}`
                          : listed.length}
                      </p>
                      <button
                        onClick={() => void navigator.clipboard?.writeText(listed.join(', '))}
                      >
                        Copy
                      </button>
                      <div class="lead-list selectable">{listed.join(', ')}</div>
                    </>
                  )}
                </div>
              );
            })}
          </section>
        </div>
      )}
      {rebuildOpen && plan && (
        <RebuildDialog
          job={id}
          plan={plan}
          onClose={onCloseRebuild}
          onRebuilt={() => setReload((value) => value + 1)}
        />
      )}
    </div>
  );
}
function compareRank(a: readonly [number, number], b: readonly [number, number]) {
  return a[0] - b[0] || a[1] - b[1];
}
function errorCode(error: unknown) {
  const parsed = EngineError.from(error);
  return parsed.code === -32601 ? "Results aren't available yet" : parsed.message;
}
