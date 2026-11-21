import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/preact';
import { Sidebar } from './Sidebar';
import { mockBridge } from '../engine/bridge';
import { projects, selectedProject, newProjectOpen, allowances, load } from '../state/store';
beforeEach(() => {
  projects.value = [
    { project: 'project-one', title: 'Project one', status: 'planned' },
    { project: 'p2', title: 'Project two', status: 'done_with_issues' },
  ];
  selectedProject.value = 'project-one';
  newProjectOpen.value = false;
  allowances.value = null;
  mockBridge.calls = [];
});
describe('Sidebar', () => {
  it('renders projects and pills', () => {
    render(<Sidebar />);
    expect(screen.getByText('Project one')).toBeInTheDocument();
    expect(screen.getByText('Done with issues')).toBeInTheDocument();
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
    await new Promise((resolve) => setTimeout(resolve));
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
