import { useEffect, useRef, useState } from 'preact/hooks';
import { phase, projects, selectedJob, selectedProject } from '../state/store';
import { bridge } from '../engine/bridge';
import { EngineError } from '../engine/client';
import { openProjectFolder } from '../engine/shellCommands';
import { ChatDrawer } from './ChatDrawer';
import { EmptyState } from './EmptyState';
import { Rail } from './Rail';
import { Pill } from './ui/Pill';
import { SetupView } from './views/SetupView';
import { SampleView } from './views/SampleView';
import { RunView } from './views/RunView';
import { DoneView } from './views/DoneView';
import { ErrorBoundary } from './ErrorBoundary';
export function MainArea() {
  const current = selectedJob.value;
  const currentProject = selectedProject.value;
  const project = projects.value.find((item) => item.project === currentProject);
  const composer = useRef<((prefix?: string) => void) | null>(null);
  const [composerRequest, setComposerRequest] = useState<{
    prefix: string;
    id: number;
  } | null>(null);
  const rebuildButton = useRef<HTMLButtonElement>(null);
  const [rebuildOpen, setRebuildOpen] = useState(false);
  const [shellError, setShellError] = useState<string | null>(null);
  const [actionPending, setActionPending] = useState(false);
  const [planError, setPlanError] = useState<unknown>(null);
  const [chat, setChat] = useState(
    () => phase.value !== 'done' && localStorage.getItem(`chat:${currentProject}`) !== 'hidden',
  );
  useEffect(
    () =>
      setChat(
        phase.value !== 'done' &&
          localStorage.getItem(`chat:${selectedProject.value}`) !== 'hidden',
      ),
    [currentProject, phase.value],
  );
  // A new phase replaces the view. If that took the focused button with it (Approve plan,
  // Start full run), start again from the heading rather than from nowhere.
  const heading = useRef<HTMLHeadingElement>(null);
  const shownPhase = useRef(phase.value);
  useEffect(() => {
    if (shownPhase.current === phase.value) return;
    shownPhase.current = phase.value;
    if (!document.activeElement || document.activeElement === document.body)
      heading.current?.focus();
  }, [phase.value]);
  if (!currentProject)
    return (
      <main class="main">
        <EmptyState />
      </main>
    );
  const state = current?.state ?? project?.status;
  const rebuildDisabledReason =
    planError || state === 'failed' || state === 'cancelled'
      ? 'Rebuild is unavailable because this job or plan failed'
      : undefined;
  const toggle = () =>
    setChat((visible) => {
      const next = !visible;
      localStorage.setItem(`chat:${currentProject}`, next ? 'shown' : 'hidden');
      return next;
    });
  const pause = () => {
    if (!current || actionPending || current.state === 'stalled') return;
    const method = current.state === 'paused' ? 'job/resume' : 'job/pause';
    setShellError(null);
    setActionPending(true);
    void bridge
      .call(method, { job: current.job })
      .catch((error) => {
        const parsed = EngineError.from(error);
        setShellError(
          parsed.reason === 'job_running' && method === 'job/resume'
            ? 'Still finishing the leads in progress. Try again in a moment.'
            : parsed.message,
        );
      })
      .finally(() => setActionPending(false));
  };
  const openFolder = () => {
    setShellError(null);
    void openProjectFolder(currentProject).catch((error) => {
      const parsed = EngineError.from(error);
      const messages: Record<string, string> = {
        file_not_found: 'That folder is no longer there',
        outside_project: 'That folder is outside the project',
        open_failed: 'The folder could not be opened',
        engine_unavailable: 'The engine is unavailable',
      };
      setShellError(
        parsed.code === -32601 || parsed.reason === 'method_not_allowed'
          ? 'Available in the desktop app'
          : (messages[parsed.reason ?? ''] ?? "Couldn't open the folder"),
      );
    });
  };
  const runAgain = () => {
    localStorage.setItem(`chat:${currentProject}`, 'shown');
    setChat(true);
    setComposerRequest({ prefix: 'Run again for new dates: ', id: Date.now() });
  };
  const view =
    phase.value === 'setup' ? (
      <SetupView id={current?.job} state={state} />
    ) : phase.value === 'sample' ? (
      <SampleView id={current!.job} state={state} />
    ) : phase.value === 'run' ? (
      <RunView
        key={current!.job}
        id={current!.job}
        onStalled={(text) => {
          setChat(true);
          setComposerRequest({
            prefix: `My job stalled: ${text} What should I do?`,
            id: Date.now(),
          });
        }}
      />
    ) : (
      <DoneView
        id={current!.job}
        project={currentProject}
        rebuildOpen={rebuildOpen}
        onPlanError={setPlanError}
        onCloseRebuild={() => {
          setRebuildOpen(false);
          rebuildButton.current?.focus();
        }}
      />
    );
  return (
    <main class="main">
      <header>
        <div>
          <h1 ref={heading} tabIndex={-1}>
            {project?.title ?? currentProject}
          </h1>
          {current ? <Pill state={state} /> : <span class="pill setup">No job yet</span>}
        </div>
        <div class="actions">
          {phase.value === 'run' && current && (
            <>
              {(current.state === 'running' || current.state === 'paused') && (
                <button type="button" disabled={actionPending} onClick={pause}>
                  {current.state === 'paused' ? 'Resume' : 'Pause'}
                </button>
              )}
              <button type="button" onClick={openFolder}>
                Open folder
              </button>
            </>
          )}
          {phase.value === 'done' && (
            <>
              <button
                ref={rebuildButton}
                type="button"
                disabled={!!rebuildDisabledReason}
                title={rebuildDisabledReason}
                onClick={() => setRebuildOpen(true)}
              >
                Rebuild with new rules
              </button>
              <button type="button" onClick={runAgain}>
                Run again with new dates
              </button>
            </>
          )}
          {shellError && (
            <span class="view-error" role="alert">
              {shellError}
            </span>
          )}
        </div>
      </header>
      <div class="body">
        <ErrorBoundary resetKey={`${currentProject}:${current?.job ?? ''}`}>
          {view}
          {(phase.value === 'setup' || phase.value === 'sample') && (
            <Rail project={currentProject} state={state} />
          )}
          {(phase.value === 'run' || phase.value === 'done') && (
            <ChatDrawer
              visible={chat}
              toggle={toggle}
              composer={composer}
              composerRequest={composerRequest}
              onComposerRequestHandled={() => setComposerRequest(null)}
            />
          )}
        </ErrorBoundary>
      </div>
    </main>
  );
}
