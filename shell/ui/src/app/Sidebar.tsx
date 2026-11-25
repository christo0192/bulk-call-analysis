import { useEffect, useRef, useState } from 'preact/hooks';
import { bridge } from '../engine/bridge';
import type { Project } from '../engine/types';
import { EngineError } from '../engine/client';
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
import { formatCount } from './format';
import { fiveHourPercent, weeklyPercent } from './allowance';

export function slugFor(title: string, existing: Project[]): string {
  const base =
    title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '') || 'project';
  let id = /^[0-9]/.test(base) ? `p-${base}` : base;
  if (id.length < 4) id += '-project';
  id = id.slice(0, 64).replace(/-$/, '');
  const root = id;
  let suffix = 2;
  while (existing.some((project) => project.project === id)) {
    const ending = `-${suffix++}`;
    id = `${root.slice(0, 64 - ending.length).replace(/-$/, '')}${ending}`;
  }
  return id;
}

export function Sidebar() {
  const [title, setTitle] = useState('');
  const [createError, setCreateError] = useState<string | null>(null);
  const open = newProjectOpen.value;
  // `autofocus` only works when the page loads, so move focus into the title box by hand.
  const titleBox = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!open) setTitle('');
    else titleBox.current?.focus();
  }, [open]);
  // The form (and the focused title box) goes away after Create; put focus on the new project
  // instead of losing it to the page.
  const projectButtons = useRef(new Map<string, HTMLButtonElement>());
  const [focusProject, setFocusProject] = useState<string | null>(null);
  useEffect(() => {
    if (!focusProject) return;
    projectButtons.current.get(focusProject)?.focus();
    setFocusProject(null);
  }, [focusProject]);
  const allowanceText = (value: string | number | undefined) =>
    value === undefined ? 'unknown' : typeof value === 'number' ? `${value}% used` : value;
  async function create() {
    const clean = title.trim();
    if (!clean) return;
    setCreateError(null);
    try {
      const project = slugFor(clean, projects.value);
      const created = await bridge.call('projects/create', { project, title: clean });
      addProject(created);
      selectedProject.value = project;
      newProjectOpen.value = false;
      setFocusProject(project);
    } catch (error) {
      setCreateError(EngineError.from(error).message);
    }
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
            ref={titleBox}
            onInput={(event) => setTitle((event.currentTarget as HTMLInputElement).value)}
          />
          {createError && <p class="error">{createError}</p>}
          <button class="primary" type="submit">
            Create
          </button>
        </form>
      )}
      <div class="side-label">Projects</div>
      <nav aria-label="Projects">
        {projects.value.map((project) => {
          const job =
            jobsByProject.value[project.project]?.find(
              (item) => !['done', 'done_with_issues', 'failed', 'cancelled'].includes(item.state),
            ) ?? jobsByProject.value[project.project]?.[0];
          const done = job?.done;
          return (
            <button
              class={`project ${selectedProject.value === project.project ? 'selected' : ''}`}
              key={project.project}
              ref={(element) => {
                if (element) projectButtons.current.set(project.project, element);
                else projectButtons.current.delete(project.project);
              }}
              onClick={() => {
                selectedProject.value = project.project;
              }}
            >
              <span>{project.title}</span>
              {job?.state === 'done' || job?.state === 'done_with_issues' ? (
                <small class="muted-status">
                  {job.state === 'done_with_issues' ? 'Done with issues' : 'Done'}
                  {done === undefined
                    ? ''
                    : ` ${String.fromCharCode(183)} ${formatCount(done)} leads`}
                </small>
              ) : job ? (
                <Pill state={job.state} />
              ) : (
                <small class="muted-status">No job yet</small>
              )}
            </button>
          );
        })}
      </nav>
      <footer>
        <div>
          <span>ChatGPT this week</span>
          <b>{allowanceText(weeklyPercent(allowances.value))}</b>
        </div>
        <div>
          <span>Claude 5-hour</span>
          <b>{allowanceText(fiveHourPercent(allowances.value))}</b>
        </div>
        <button class="settings" onClick={() => (settingsOpen.value = true)}>
          <GearIcon /> Settings
        </button>
      </footer>
    </aside>
  );
}
