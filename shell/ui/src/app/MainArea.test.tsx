import { beforeEach, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { MainArea } from './MainArea';
import {
  jobsByProject,
  projects,
  selectedProject,
  newProjectOpen,
  addProject,
} from '../state/store';
import { mockBridge } from '../engine/bridge';
beforeEach(() => {
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
  fireEvent.click(screen.getByRole('button', { name: /Hide/ }));
  expect(screen.getByRole('button', { name: /Chat/ })).toBeInTheDocument();
  expect(localStorage.getItem('chat:p')).toBe('hidden');
});
it('header pause calls the job action once', async () => {
  jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'running' }] };
  render(<MainArea />);
  await waitFor(() => expect(screen.getByTestId('run-view')).toBeInTheDocument());
  fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
  expect(mockBridge.calls.filter((call) => call.method === 'job/pause')).toHaveLength(1);
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
