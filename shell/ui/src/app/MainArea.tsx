import { useEffect, useState } from 'preact/hooks';
import { phase, projects, selectedJob, selectedProject } from '../state/store';
import { ChatDrawer } from './ChatDrawer';
import { EmptyState } from './EmptyState';
import { Rail } from './Rail';
import { Pill } from './ui/Pill';
import { SetupView } from './views/SetupView';
import { SampleView } from './views/SampleView';
import { RunView } from './views/RunView';
import { DoneView } from './views/DoneView';
export function MainArea() {
  const current = selectedJob.value;
  const currentProject = selectedProject.value;
  const project = projects.value.find((item) => item.project === currentProject);
  const [chat, setChat] = useState(
    () => localStorage.getItem(`chat:${currentProject}`) !== 'hidden',
  );
  useEffect(
    () => setChat(localStorage.getItem(`chat:${selectedProject.value}`) !== 'hidden'),
    [currentProject],
  );
  if (!current || !currentProject)
    return (
      <main class="main">
        <EmptyState />
      </main>
    );
  const toggle = () =>
    setChat((visible) => {
      const next = !visible;
      localStorage.setItem(`chat:${currentProject}`, next ? 'shown' : 'hidden');
      return next;
    });
  const view =
    phase.value === 'setup' ? (
      <SetupView id={current.job} />
    ) : phase.value === 'sample' ? (
      <SampleView id={current.job} />
    ) : phase.value === 'run' ? (
      <RunView id={current.job} />
    ) : (
      <DoneView id={current.job} />
    );
  return (
    <main class="main">
      <header>
        <div>
          <h1>{project?.title ?? currentProject}</h1>
          <Pill state={current.state} />
        </div>
        <div class="actions" />
      </header>
      <div class="body">
        {view}
        {(phase.value === 'setup' || phase.value === 'sample') && (
          <Rail project={currentProject} state={current.state} />
        )}
        {(phase.value === 'run' || phase.value === 'done') && (
          <ChatDrawer visible={chat} toggle={toggle} />
        )}
      </div>
    </main>
  );
}
