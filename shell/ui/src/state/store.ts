import { computed, signal } from '@preact/signals';
import type {
  Allowances,
  JobSummary,
  Project,
  Notification,
  SetupItem,
  AuthStatus,
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
export const loadError = signal<string | null>(null);
export type Capability = 'available' | 'unknown' | 'missing';
export const capabilities = signal<Record<string, Capability>>({});
let removeNotification: (() => void) | undefined;
let removeStatus: (() => void) | undefined;
let loadPromise: Promise<void> | null = null;
let loadAttempted = false;

export function addProject(project: Project): void {
  projects.value = [
    ...projects.value.filter((existing) => existing.project !== project.project),
    project,
  ];
}
export const selectedJob = computed(() => {
  const id = selectedProject.value;
  if (!id) return null;
  const jobs = jobsByProject.value[id] ?? [];
  return (
    jobs.find((job) => !['done', 'done_with_issues', 'failed', 'cancelled'].includes(job.state)) ??
    jobs[0] ??
    null
  );
});
export function phaseFor(state?: string): 'setup' | 'sample' | 'run' | 'done' {
  if (state === 'sample_ready' || state === 'sample_approved') return 'sample';
  if (
    state === 'running' ||
    state === 'paused' ||
    state === 'stalled' ||
    [
      'pulling',
      'masking_sample',
      'sample_running',
      'full_running',
      'quote_check',
      'building_output',
      'verifying',
    ].includes(state ?? '')
  )
    return 'run';
  if (state === 'done' || state === 'done_with_issues') return 'done';
  return 'setup';
}
export const phase = computed(() => phaseFor(selectedJob.value?.state));
function itemReady(item: SetupItem, auth: AuthStatus): boolean {
  return (
    item.status === 'ok' ||
    item.status === 'manual_done' ||
    (item.name === 'claude_login' && auth.claude.loggedIn) ||
    (item.name === 'chatgpt_login' && auth.codex.loggedIn)
  );
}

async function refetchJobs(names: string[]): Promise<void> {
  if (capabilities.value['projects/jobs'] === 'missing') return;
  await Promise.all(
    names.map(async (project) => {
      try {
        const result = await bridge.call('projects/jobs', { project });
        capabilities.value = { ...capabilities.value, 'projects/jobs': 'available' };
        jobsByProject.value = { ...jobsByProject.value, [project]: result.jobs };
      } catch (error) {
        if (EngineError.from(error).code === -32601) {
          capabilities.value = { ...capabilities.value, 'projects/jobs': 'missing' };
          jobsByProject.value = { ...jobsByProject.value, [project]: [] };
        } else console.warn(`Unable to refresh jobs: ${EngineError.from(error).message}`);
      }
    }),
  );
}
export function reduceNotification(n: Notification): void {
  if (n.method === 'job/state') {
    const p = n.params as { job?: string; state?: string; project?: string };
    if (!p.job || !p.state) return;
    const next = { ...jobsByProject.value };
    let found = false;
    for (const [project, items] of Object.entries(next)) {
      if (items.some((job) => job.job === p.job)) {
        found = true;
        next[project] = items.map((job) =>
          job.job === p.job ? { ...job, state: p.state as JobSummary['state'] } : job,
        );
      }
    }
    jobsByProject.value = next;
    if (!found)
      void refetchJobs(p.project ? [p.project] : projects.value.map((project) => project.project));
  } else if (n.method === 'job/progress') {
    const p = n.params as {
      job?: string;
      done?: number;
      total?: number;
      remaining?: number | string;
    };
    if (!p.job) return;
    const next = { ...jobsByProject.value };
    for (const [project, items] of Object.entries(next))
      next[project] = items.map((job) =>
        job.job === p.job
          ? {
              ...job,
              ...(Number.isInteger(p.done) ? { done: p.done } : {}),
              ...(Number.isInteger(p.total) ? { total: p.total } : {}),
              ...(Number.isNaN(Number(p.remaining)) ? {} : { remaining: Number(p.remaining) }),
            }
          : job,
      );
    jobsByProject.value = next;
  }
}
function acceptStatus(status: string): boolean {
  return ['starting', 'ready', 'restarting', 'stopped'].includes(status);
}
export async function load(): Promise<void> {
  if (loadPromise) return loadPromise;
  loadPromise = loadOnce();
  try {
    await loadPromise;
  } finally {
    loadPromise = null;
  }
}
async function loadOnce(): Promise<void> {
  loadAttempted = true;
  loadError.value = null;
  engineStatus.value = 'starting';
  if (!removeNotification) removeNotification = bridge.onNotification(reduceNotification);
  if (!removeStatus)
    removeStatus = bridge.onStatus(({ status }) => {
      if (acceptStatus(status)) {
        const wasReady = engineStatus.value === 'ready';
        engineStatus.value = status as typeof engineStatus.value;
        if (status === 'ready' && !wasReady && loadAttempted && !loadPromise) void load();
      } else if (status === 'error') engineStatus.value = 'failed';
      else console.warn(`Unknown engine status: ${status}`);
    });
  try {
    const initialStatus = await bridge.status();
    if (initialStatus === 'error') engineStatus.value = 'failed';
    else if (acceptStatus(initialStatus))
      engineStatus.value = initialStatus as typeof engineStatus.value;
    setupItems.value = await bridge.call('setup/checklist', {});
    projects.value = await bridge.call('projects/list', {});
    selectedProject.value ??= projects.value[0]?.project ?? null;
    await refetchJobs(projects.value.map((project) => project.project));
    try {
      allowances.value = await bridge.call('allowance/status', {});
      capabilities.value = { ...capabilities.value, 'allowance/status': 'available' };
    } catch (error) {
      if (EngineError.from(error).code !== -32601) throw error;
      capabilities.value = { ...capabilities.value, 'allowance/status': 'missing' };
      allowances.value = null;
    }
    try {
      const auth = await bridge.call('auth/status', {});
      setupRequired.value = setupItems.value.some((item) => !itemReady(item, auth));
    } catch (authError) {
      console.warn(`Unable to load authentication status: ${EngineError.from(authError).message}`);
      const signedOut: AuthStatus = { claude: { loggedIn: false }, codex: { loggedIn: false } };
      setupRequired.value = setupItems.value.some((item) => !itemReady(item, signedOut));
    }
  } catch (error) {
    const e = EngineError.from(error);
    if (e.code === -32002) {
      setupRequired.value = true;
      try {
        setupItems.value = await bridge.call('setup/checklist', {});
      } catch (checkError) {
        console.warn(`Unable to load setup checklist: ${EngineError.from(checkError).message}`);
      }
      try {
        await bridge.call('auth/status', {});
      } catch (authError) {
        console.warn(
          `Unable to load authentication status: ${EngineError.from(authError).message}`,
        );
      }
    } else if (e.code === null) engineStatus.value = 'failed';
    else {
      if (e.code === -32601)
        capabilities.value = { ...capabilities.value, 'projects/list': 'missing' };
      loadError.value = e.message;
      console.warn(`Unable to load engine data: ${e.message}`);
    }
  }
}
