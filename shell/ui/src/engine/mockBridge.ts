import type { EngineBridge, Unsubscribe } from './client';
import { EngineError } from './client';
import type { Method, ParamsOf, ResultOf, Notification } from './types';
import {
  projects,
  jobs,
  allowances,
  files,
  checklist,
  authStatus,
  settings,
  appStatus,
  newComputerChecklist,
} from '../mock/fixtures/data';
const firstRun = typeof window !== 'undefined' && window.location.search.includes('mock=firstrun');
const noAuth = typeof window !== 'undefined' && window.location.search.includes('mock=noauth');
let firstRunActive = firstRun;
const checklistState = (firstRun ? newComputerChecklist : checklist).map((item) => ({
  ...item,
  ...(item.name === 'claude_login' || item.name === 'chatgpt_login'
    ? { status: 'failed', detail: 'probe not supplied' }
    : {}),
}));
const authState = {
  // In the first-run mock, Claude starts signed out until the sign-in flow completes.
  claude: {
    ...authStatus.claude,
    loggedIn: noAuth || firstRun ? false : authStatus.claude.loggedIn,
  },
  codex: { ...authStatus.codex, loggedIn: noAuth || firstRun ? false : authStatus.codex.loggedIn },
};
const settingsState = { ...settings };
type ResponseFn = (params: unknown) => unknown | Promise<unknown>;
export class MockBridge implements EngineBridge {
  calls: Array<{ method: Method; params: unknown }> = [];
  private responses = new Map<Method, ResponseFn>();
  private notifications = new Set<(n: Notification) => void>();
  private statuses = new Set<(s: { status: string; reason?: string }) => void>();
  private checkCount = 0;
  private signInStarted = !firstRun;
  async call<M extends Method>(method: M, params: ParamsOf<M>): Promise<ResultOf<M>> {
    this.calls.push({ method, params });
    const custom = this.responses.get(method);
    if (custom) return (await custom(params)) as ResultOf<M>;
    if (method === 'projects/list') {
      if (firstRunActive) throw 'RPC error -32002: Setup required';
      return projects as ResultOf<M>;
    }
    if (method === 'projects/jobs')
      return {
        jobs: jobs.filter((j) => j.project === (params as { project: string }).project),
      } as ResultOf<M>;
    if (method === 'projects/create') {
      const p = params as { project: string; title: string };
      const project = { ...p, status: 'planned' as const };
      const existing = projects.findIndex((item) => item.project === project.project);
      if (existing >= 0) projects[existing] = project;
      else projects.push(project);
      jobs.push({
        job: `synthetic-job-${jobs.length + 1}`,
        project: p.project,
        state: 'planned',
        total: 11058,
        done: 0,
        remaining: 11058,
      });
      return project as ResultOf<M>;
    }
    if (method === 'allowance/status') return allowances as ResultOf<M>;
    if (method === 'setup/checklist') return checklistState as ResultOf<M>;
    if (method === 'setup/runChecks') {
      // Sign-in completes on the second check AFTER auth/claudeLogin was called.
      if (this.signInStarted) this.checkCount += 1;
      if (this.checkCount >= 2) {
        const item = checklistState.find((entry) => entry.name === 'claude_login');
        if (item) item.status = 'ok';
        authState.claude.loggedIn = true;
        const codexItem = checklistState.find((entry) => entry.name === 'chatgpt_login');
        if (codexItem) codexItem.status = 'ok';
        authState.codex.loggedIn = true;
      }
      return { items: checklistState } as ResultOf<M>;
    }
    if (method === 'setup/markManual') {
      const name = (params as { item: string }).item;
      const item = checklistState.find((entry) => entry.name === name);
      if (item) item.status = 'manual_done';
      if (firstRun && name === 'extra_usage_off') firstRunActive = false;
      return { item: name, status: item?.status ?? 'manual_done' } as ResultOf<M>;
    }
    if (method === 'auth/status') {
      if (noAuth) throw 'RPC error -32001: Codex app-server did not start';
      return authState as ResultOf<M>;
    }
    if (method === 'auth/claudeLogin') {
      this.signInStarted = true;
      return { started: true, vendor: 'claude' } as ResultOf<M>;
    }
    if (method === 'auth/codexLogin') return { started: true, vendor: 'codex' } as ResultOf<M>;
    if (method === 'settings/get') return settingsState as ResultOf<M>;
    if (method === 'settings/set') {
      Object.assign(settingsState, params);
      return settingsState as ResultOf<M>;
    }
    if (method === 'app/status') return appStatus as ResultOf<M>;
    if (method === 'files/list') return files as ResultOf<M>;
    if (method === 'job/status')
      return (
        jobs.find((j) => j.job === (params as { job: string }).job) ?? (jobs[0] as ResultOf<M>)
      );
    if (method === 'health/ping') return { status: 'ok' } as ResultOf<M>;
    return {} as ResultOf<M>;
  }
  setResponse<M extends Method>(
    method: M,
    fn: (params: ParamsOf<M>) => ResultOf<M> | Promise<ResultOf<M>>,
  ): void {
    this.responses.set(method, fn as ResponseFn);
  }
  /** Test helper: forget every response set with setResponse, so tests stay isolated. */
  clearResponses(): void {
    this.responses.clear();
  }
  emit(notification: Notification): void {
    this.notifications.forEach((cb) => cb(notification));
  }
  onNotification(cb: (n: Notification) => void): Unsubscribe {
    this.notifications.add(cb);
    return () => this.notifications.delete(cb);
  }
  onStatus(cb: (s: { status: string; reason?: string }) => void): Unsubscribe {
    this.statuses.add(cb);
    return () => this.statuses.delete(cb);
  }
  emitStatus(status: string, reason?: string): void {
    this.statuses.forEach((cb) => cb({ status, reason }));
  }
  status = async () => 'ready';
}
export const mockBridge = new MockBridge();
