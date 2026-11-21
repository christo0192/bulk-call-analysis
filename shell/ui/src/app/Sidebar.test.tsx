import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/preact';
import { Sidebar } from './Sidebar';
import { mockBridge } from '../engine/bridge';
import {
  projects,
  selectedProject,
  newProjectOpen,
  allowances,
  jobsByProject,
  load,
} from '../state/store';
beforeEach(() => {
  projects.value = [
    { project: 'project-one', title: 'Project one', status: 'planned' },
    { project: 'p2', title: 'Project two', status: 'done_with_issues' },
  ];
  selectedProject.value = 'project-one';
  jobsByProject.value = {};
  newProjectOpen.value = false;
  allowances.value = null;
  mockBridge.calls = [];
  // A test that makes projects/create fail must not leak into the next test.
  mockBridge.clearResponses();
});
describe('Sidebar', () => {
  it('renders projects and pills', () => {
    jobsByProject.value = {
      'project-one': [{ job: 'j1', project: 'project-one', state: 'planned' }],
      p2: [{ job: 'j2', project: 'p2', state: 'done_with_issues', done: 2 }],
    };
    render(<Sidebar />);
    expect(screen.getByText('Project one')).toBeInTheDocument();
    // The done line reads "Done with issues · 2 leads" in one text node.
    expect(screen.getByText(/^Done with issues/)).toBeInTheDocument();
  });
  it('sidebar pill comes from the active job and updates on job/state', async () => {
    jobsByProject.value = {
      'project-one': [
        { job: 'old', project: 'project-one', state: 'done' },
        { job: 'active', project: 'project-one', state: 'running' },
      ],
    };
    render(<Sidebar />);
    expect(screen.getByText('Reading')).toBeInTheDocument();
    jobsByProject.value = {
      'project-one': [{ job: 'active', project: 'project-one', state: 'done_with_issues' }],
    };
    await new Promise((resolve) => setTimeout(resolve));
    expect(screen.getByText('Done with issues')).toBeInTheDocument();
  });
  it('a project with no jobs shows No job yet', () => {
    jobsByProject.value = {
      p2: [{ job: 'j2', project: 'p2', state: 'planned' }],
    };
    render(<Sidebar />);
    const projectOne = screen.getByText('Project one').closest('button');
    if (!projectOne) throw new Error('Project one button not found');
    expect(within(projectOne).getByText('No job yet')).toBeInTheDocument();
  });
  it('slugFor makes valid engine ids', async () => {
    const { slugFor } = await import('./Sidebar');
    const existing = [{ project: 'ab-project', title: 'Existing' }];
    const values = ['2026 review', 'AB', 'a'.repeat(100), 'AB'];
    const result = values.map((value) => {
      const id = slugFor(value, existing);
      existing.push({ project: id, title: value });
      return id;
    });
    expect(result.every((id) => /^[a-z][a-z0-9-]{2,62}[a-z0-9]$/.test(id))).toBe(true);
    expect(new Set(result).size).toBe(result.length);
  });
  it('a failed create shows the engine message and keeps the form open', async () => {
    mockBridge.setResponse('projects/create', async () => {
      throw 'RPC error -32001: Cannot create project';
    });
    render(<Sidebar />);
    fireEvent.click(screen.getByRole('button', { name: /New project/ }));
    fireEvent.input(screen.getByLabelText('Project title'), {
      target: { value: 'Broken project' },
    });
    const form = screen.getByRole('button', { name: 'Create' }).closest('form');
    if (!form) throw new Error('create form not found');
    fireEvent.submit(form);
    expect(await screen.findByText('Cannot create project')).toBeInTheDocument();
    expect(screen.getByLabelText('Project title')).toBeInTheDocument();
  });
  it('creates a unique slug', async () => {
    render(<Sidebar />);
    fireEvent.click(screen.getByRole('button', { name: /New project/ }));
    fireEvent.input(screen.getByLabelText('Project title'), { target: { value: 'Project one' } });
    // happy-dom does not forward a submit event dispatched on the button to its form,
    // so submit the form itself (the Playwright flow covers a real click on Create).
    const form = screen.getByRole('button', { name: 'Create' }).closest('form');
    if (!form) throw new Error('create form not found');
    fireEvent.submit(form);
    await new Promise((resolve) => setTimeout(resolve));
    expect(mockBridge.calls).toContainEqual({
      method: 'projects/create',
      params: { project: 'project-one-2', title: 'Project one' },
    });
  });
  it('keeps a created project unique after reloading projects', async () => {
    render(<Sidebar />);
    fireEvent.click(screen.getByRole('button', { name: /New project/ }));
    fireEvent.input(screen.getByLabelText('Project title'), {
      target: { value: 'Synthetic reload project' },
    });
    const form = screen.getByRole('button', { name: 'Create' }).closest('form');
    if (!form) throw new Error('create form not found');
    fireEvent.submit(form);
    await waitFor(() =>
      expect(projects.value.some((project) => project.title === 'Synthetic reload project')).toBe(
        true,
      ),
    );
    await load();
    expect(
      projects.value.filter((project) => project.title === 'Synthetic reload project'),
    ).toHaveLength(1);
  });
  it('selects project', () => {
    render(<Sidebar />);
    fireEvent.click(screen.getByText('Project two'));
    expect(selectedProject.value).toBe('p2');
  });
  it('opens and closes the form', () => {
    render(<Sidebar />);
    fireEvent.click(screen.getByRole('button', { name: /New project/ }));
    expect(screen.getByLabelText('Project title')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /New project/ }));
    expect(screen.queryByLabelText('Project title')).toBeNull();
  });
});
