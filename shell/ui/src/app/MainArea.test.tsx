import { beforeEach, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/preact';
import { MainArea } from './MainArea';
import { jobsByProject, projects, selectedProject, newProjectOpen } from '../state/store';
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
