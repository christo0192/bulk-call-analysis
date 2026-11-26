import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { beforeEach, expect, it, vi } from 'vitest';
import { MainArea } from './MainArea';
import {
  jobsByProject,
  projects,
  selectedProject,
  newProjectOpen,
  addProject,
} from '../state/store';
import { mockBridge } from '../engine/mockBridge';
import { EngineError } from '../engine/client';
import { openProjectFolder } from '../engine/shellCommands';

vi.mock('../engine/shellCommands', () => ({
  openProjectFolder: vi.fn(),
  openProjectFile: vi.fn(),
}));

beforeEach(() => {
  mockBridge.resetForTest();
  projects.value = [{ project: 'p', title: 'Project one', status: 'planned' }];
  selectedProject.value = 'p';
  jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'planned' }] };
  newProjectOpen.value = false;
  localStorage.clear();
});
it('renders setup view and rail', () => {
  render(<MainArea />);
  expect(screen.getByTestId('setup-view')).toBeInTheDocument();
  expect(screen.getByText('Files')).toBeInTheDocument();
});
it('empty-state button opens the form signal', () => {
  selectedProject.value = null;
  jobsByProject.value = {};
  render(<MainArea />);
  fireEvent.click(screen.getByRole('button', { name: 'New project' }));
  expect(newProjectOpen.value).toBe(true);
});
it('drawer hides and restores', () => {
  jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'running' }] };
  render(<MainArea />);
  fireEvent.click(screen.getByRole('button', { name: 'Hide chat' }));
  expect(screen.getByRole('button', { name: 'Show chat' })).toBeInTheDocument();
  expect(localStorage.getItem('chat:p')).toBe('hidden');
});
it('pause is sent once and errors are shown', async () => {
  jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'running' }] };
  mockBridge.setResponse('job/pause', async () => {
    throw new EngineError(-32000, 'Pause failed', 'pause_failed');
  });
  render(<MainArea />);
  await waitFor(() => expect(screen.getByTestId('run-view')).toBeInTheDocument());
  fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
  expect(mockBridge.calls.filter((call) => call.method === 'job/pause')).toHaveLength(1);
  expect(await screen.findByRole('alert')).toHaveTextContent('Pause failed');
});

it('stalled job offers no pause', async () => {
  jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'stalled' }] };
  mockBridge.setResponse(
    'job/snapshot',
    async () =>
      ({
        job: 'j',
        project: 'p',
        state: 'stalled',
        stall: { reason: 'pull_failed', text: 'Pull failed' },
        progress: {
          phase: 'full',
          done: 1,
          inFlight: 0,
          retrying: 0,
          failed: 0,
          heldBack: 0,
          remaining: 1,
          total: 2,
        },
        counts: {
          selected: 2,
          usable: 2,
          noTranscript: 0,
          transcriptionFailed: 0,
          tooShort: 0,
          notFound: 0,
        },
        quota: { codex: null, projectedEndPercent: null },
        resumeAt: null,
        pauseReason: null,
      }) as never,
  );
  render(<MainArea />);
  await waitFor(() => expect(screen.getByTestId('run-view')).toBeInTheDocument());
  expect(screen.queryByRole('button', { name: 'Pause' })).toBeNull();
});

it('open folder shows the real reason', async () => {
  jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'running' }] };
  vi.mocked(openProjectFolder).mockRejectedValueOnce(
    new EngineError(-32000, 'outside', 'outside_project'),
  );
  render(<MainArea />);
  fireEvent.click(await screen.findByRole('button', { name: 'Open folder' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('That folder is outside the project');
});

it('resume while draining says to try again', async () => {
  jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'paused' }] };
  mockBridge.setResponse('job/resume', async () => {
    throw new EngineError(-32000, 'busy', 'job_running');
  });
  render(<MainArea />);
  fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Still finishing the leads in progress. Try again in a moment.',
  );
});
it('after creating a project the setup view shows instead of No projects yet', async () => {
  projects.value = [];
  selectedProject.value = null;
  jobsByProject.value = {};
  render(<MainArea />);
  addProject({ project: 'new-project', title: 'New project', status: 'planned' });
  selectedProject.value = 'new-project';
  jobsByProject.value = {
    'new-project': [{ job: 'new-job', project: 'new-project', state: 'planned' }],
  };
  await waitFor(() => expect(screen.getByTestId('setup-view')).toBeInTheDocument());
  expect(screen.queryByText('No projects yet')).toBeNull();
});
it('a selected project without a job still shows the setup view', async () => {
  projects.value = [{ project: 'new-project', title: 'New project', status: 'planned' }];
  selectedProject.value = 'new-project';
  jobsByProject.value = {};
  render(<MainArea />);
  await waitFor(() => expect(screen.getByTestId('setup-view')).toBeInTheDocument());
  expect(screen.queryByText('No projects yet')).toBeNull();
});
