import { computed, signal } from '@preact/signals';
import type {
  Allowances,
  JobSummary,
  Project,
  JobState,
  Notification,
  SetupItem,
} from '../engine/types';
import { bridge } from '../engine/bridge';
import { EngineError } from '../engine/client';
export const engineStatus = signal<'starting' | 'ready' | 'restarting' | 'failed' | 'stopped'>(
  'starting',
);
export const projects = signal<Project[]>([]);
export const selectedProject = signal<string | null>(null);
export const jobsByProject = signal<Record<string, JobSummary[]>>({});
export const allowances = signal<Allowances | null>(null);
export const newProjectOpen = signal(false);
export const setupRequired = signal(false);
export const settingsOpen = signal(false);
export const setupItems = signal<SetupItem[]>([]);
let removeNotification: (() => void) | undefined;
let removeStatus: (() => void) | undefined;
export function addProject(project: Project): void {
  projects.value = [
    ...projects.value.filter((existing) => existing.project !== project.project),
    project,
  ];
}
export const selectedJob = computed(() => {
  const id = selectedProject.value;
  return id ? (jobsByProject.value[id]?.[0] ?? null) : null;
});
export function phaseFor(state?: JobState): 'setup' | 'sample' | 'run' | 'done' {
  if (state === 'sample_ready') return 'sample';
  if (state === 'running' || state === 'paused' || state === 'stalled') return 'run';
  if (state === 'done' || state === 'done_with_issues') return 'done';
  return 'setup';
}
export const phase = computed(() => phaseFor(selectedJob.value?.state));
async function refetchJobs(projectNames: string[]): Promise<void> {
  await Promise.all(
    projectNames.map(async (project) => {
      const result = await bridge.call('projects/jobs', { project });
      jobsByProject.value = { ...jobsByProject.value, [project]: result.jobs };
    }),
  );
}
export function reduceNotification(n: Notification): void {
  if (n.method === 'job/state') {
    const p = n.params as { job?: string; state?: JobState; project?: string };
    if (!p.job || !p.state) return;
    const next = { ...jobsByProject.value };
    let found = false;
    for (const [project, items] of Object.entries(next)) {
      const index = items.findIndex((j) => j.job === p.job);
      if (index >= 0) {
        found = true;
        next[project] = items.map((j, i) => (i === index ? { ...j, state: p.state! } : j));
      }
    }
    jobsByProject.value = next;
    if (!found)
      void refetchJobs(p.project ? [p.project] : projects.value.map((project) => project.project));
  } else if (n.method === 'job/progress') {
    const p = n.params as { job?: string; done?: number; total?: number; remaining?: number };
    if (!p.job) return;
    const next = { ...jobsByProject.value };
    for (const [project, items] of Object.entries(next))
      next[project] = items.map((j) =>
        j.job === p.job
          ? {
              ...j,
              ...(Number.isInteger(p.done) ? { done: p.done } : {}),
              ...(Number.isInteger(p.total) ? { total: p.total } : {}),
              ...(Number.isInteger(p.remaining) ? { remaining: p.remaining } : {}),
            }
          : j,
      );
    jobsByProject.value = next;
  }
}
export async function load(): Promise<void> {
  engineStatus.value = 'starting';
  if (!removeNotification) removeNotification = bridge.onNotification(reduceNotification);
  if (!removeStatus)
    removeStatus = bridge.onStatus(({ status }) => {
      if (
        status === 'starting' ||
        status === 'ready' ||
        status === 'restarting' ||
        status === 'stopped'
      ) {
        engineStatus.value = status;
      } else if (status === 'error') {
        engineStatus.value = 'failed';
      } else {
        console.warn(`Unknown engine status: ${status}`);
      }
    });
  try {
    const initialStatus = await bridge.status();
    if (initialStatus === 'error') engineStatus.value = 'failed';
    else if (
      initialStatus === 'starting' ||
      initialStatus === 'ready' ||
      initialStatus === 'restarting' ||
      initialStatus === 'stopped'
    )
      engineStatus.value = initialStatus;
    else console.warn(`Unknown engine status: ${initialStatus}`);
    setupItems.value = await bridge.call('setup/checklist', {});
    setupRequired.value = setupItems.value.some(
      (item) => item.status !== 'ok' && item.status !== 'manual_done',
    );
    projects.value = await bridge.call('projects/list', {});
    selectedProject.value ??= projects.value[0]?.project ?? null;
    await refetchJobs(projects.value.map((project) => project.project));
    allowances.value = await bridge.call('allowance/status', {});
  } catch (error) {
    if (EngineError.from(error).code === -32002) {
      setupRequired.value = true;
      try {
        setupItems.value = await bridge.call('setup/checklist', {});
      } catch (checkError) {
        console.warn(`Unable to load setup checklist: ${EngineError.from(checkError).message}`);
      }
    } else engineStatus.value = 'failed';
  }
}
