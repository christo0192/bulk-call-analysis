import { useEffect, useRef, useState } from 'preact/hooks';
import { phase, projects, selectedJob, selectedProject } from '../state/store';
import { bridge } from '../engine/bridge';
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
  const rebuildButton = useRef<HTMLButtonElement>(null);
  const [rebuildOpen, setRebuildOpen] = useState(false);
  const [shellError, setShellError] = useState<string | null>(null);
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
  if (!currentProject)
    return (
      <main class="main">
        <EmptyState />
      </main>
    );
  const state = current?.state ?? project?.status;
  const toggle = () =>
    setChat((visible) => {
      const next = !visible;
      localStorage.setItem(`chat:${currentProject}`, next ? 'shown' : 'hidden');
      return next;
    });
  const pause = () => {
    if (current)
      void bridge.call(current.state === 'paused' ? 'job/resume' : 'job/pause', {
        job: current.job,
      });
  };
  const openFolder = () => {
    setShellError(null);
    void openProjectFolder(currentProject).catch((error) => {
      const message = String(error);
      setShellError(
        message.includes('type_not_allowed')
          ? "The app can't open that file type"
          : 'That folder is no longer there',
      );
    });
  };
  const view =
    phase.value === 'setup' ? (
      <SetupView id={current?.job} state={state} />
    ) : phase.value === 'sample' ? (
      <SampleView id={current!.job} state={state} />
    ) : phase.value === 'run' ? (
      <RunView id={current!.job} showActions={false} />
    ) : (
      <DoneView
        id={current!.job}
        project={currentProject}
        rebuildOpen={rebuildOpen}
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
          <h1>{project?.title ?? currentProject}</h1>
          {current ? <Pill state={state} /> : <span class="pill setup">No job yet</span>}
        </div>
        <div class="actions">
          {phase.value === 'run' && current && (
            <>
              <button type="button" onClick={pause}>
                {current.state === 'paused' ? 'Resume' : 'Pause'}
              </button>
              <button type="button" onClick={openFolder}>
                Open folder
              </button>
            </>
          )}
          {phase.value === 'done' && (
            <>
              <button ref={rebuildButton} type="button" onClick={() => setRebuildOpen(true)}>
                Rebuild with new rules
              </button>
              <button type="button" onClick={() => composer.current?.('Run again for new dates: ')}>
                Run again with new dates
              </button>
            </>
          )}
        </div>
      </header>
      {shellError && (
        <div class="view-error" role="alert">
          {shellError}
        </div>
      )}
      <div class="body">
        <ErrorBoundary resetKey={`${currentProject}:${current?.job ?? ''}`}>
          {view}
          {(phase.value === 'setup' || phase.value === 'sample') && (
            <Rail project={currentProject} state={state} />
          )}
          {(phase.value === 'run' || phase.value === 'done') && (
            <ChatDrawer visible={chat} toggle={toggle} composer={composer} />
          )}
        </ErrorBoundary>
      </div>
    </main>
  );
}
