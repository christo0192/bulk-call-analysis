import { useEffect, useState } from 'preact/hooks';
import { bridge } from '../engine/bridge';
import type { Project } from '../engine/types';
import {
  allowances,
  addProject,
  jobsByProject,
  newProjectOpen,
  projects,
  selectedProject,
  settingsOpen,
} from '../state/store';
import { BrandIcon, GearIcon } from './ui/icons';
import { Pill } from './ui/Pill';

function slugFor(title: string, existing: Project[]): string {
  const base =
    title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '') || 'project';
  let id = base;
  let suffix = 2;
  while (existing.some((project) => project.project === id)) id = `${base}-${suffix++}`;
  return id;
}

export function Sidebar() {
  const [title, setTitle] = useState('');
  const open = newProjectOpen.value;
  useEffect(() => {
    if (!open) setTitle('');
  }, [open]);
  const allowanceText = (value: string | number | undefined) =>
    value === undefined ? 'unknown' : typeof value === 'number' ? `${value}% used` : value;
  async function create() {
    const clean = title.trim();
    if (!clean) return;
    const project = slugFor(clean, projects.value);
    const created = await bridge.call('projects/create', { project, title: clean });
    addProject(created);
    selectedProject.value = project;
    newProjectOpen.value = false;
  }
  return (
    <aside class="sidebar">
      <div class="brand">
        <span class="logo">
          <BrandIcon />
        </span>
        <span>Bulk Call Analysis</span>
      </div>
      <button
        class="new-project"
        onClick={() => {
          newProjectOpen.value = !open;
        }}
      >
        + New project
      </button>
      {open && (
        <form
          class="new-form"
          onSubmit={(event) => {
            event.preventDefault();
            void create();
          }}
        >
          <label htmlFor="project-title">Project title</label>
          <input
            id="project-title"
            value={title}
            onInput={(event) => setTitle((event.currentTarget as HTMLInputElement).value)}
            autoFocus
          />
          <button class="primary" type="submit">
            Create
          </button>
        </form>
      )}
      <div class="side-label">Projects</div>
      <nav aria-label="Projects">
        {projects.value.map((project) => {
          const done = jobsByProject.value[project.project]?.[0]?.done;
          return (
            <button
              class={`project ${selectedProject.value === project.project ? 'selected' : ''}`}
              key={project.project}
              onClick={() => {
                selectedProject.value = project.project;
              }}
            >
              <span>{project.title}</span>
              {project.status === 'done' || project.status === 'done_with_issues' ? (
                <small class="muted-status">
                  {project.status === 'done_with_issues' ? 'Done with issues' : 'Done'}
                  {done === undefined
                    ? ''
                    : ` ${String.fromCharCode(183)} ${done.toLocaleString()} leads`}
                </small>
              ) : (
                <Pill state={project.status} />
              )}
            </button>
          );
        })}
      </nav>
      <footer>
        <div>
          <span>ChatGPT this week</span>
          <b>{allowanceText(allowances.value?.chatgpt)}</b>
        </div>
        <div>
          <span>Claude 5-hour</span>
          <b>{allowanceText(allowances.value?.claude)}</b>
        </div>
        <button class="settings" onClick={() => (settingsOpen.value = true)}>
          <GearIcon /> Settings
        </button>
      </footer>
    </aside>
  );
}
