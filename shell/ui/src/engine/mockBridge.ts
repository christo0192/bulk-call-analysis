import type { EngineBridge, Unsubscribe } from './client';
import { EngineError } from './client';
import type { Method, ParamsOf, ResultOf, Notification, ChatEntry } from './types';
import type { JobOutputs, RebuildResult } from './types';
import outputsFixture from '../mock/fixtures/engine/job-outputs.json';
import runningSnapshot from '../mock/fixtures/engine/job-snapshot-running.json';
import pausedSnapshot from '../mock/fixtures/engine/job-snapshot-paused.json';
import pausedQuotaSnapshot from '../mock/fixtures/engine/job-snapshot-paused-quota.json';
import recordedNotifications from '../mock/fixtures/engine/notifications.json';
import allowanceFixture from '../mock/fixtures/engine/allowance-status.json';
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
  plan,
  planWithEstimate,
  sample,
} from '../mock/fixtures/data';
const firstRun = typeof window !== 'undefined' && window.location.search.includes('mock=firstrun');
const noAuth = typeof window !== 'undefined' && window.location.search.includes('mock=noauth');
const quotaPause =
  typeof window !== 'undefined' && window.location.search.includes('mock=quota-pause');
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
  shellCalls: Array<{ command: string; params: unknown }> = [];
  private responses = new Map<Method, ResponseFn>();
  private notifications = new Set<(n: Notification) => void>();
  private statuses = new Set<(s: { status: string; reason?: string }) => void>();
  private checkCount = 0;
  private signInStarted = !firstRun;
  private runProgressTimer: ReturnType<typeof setTimeout> | undefined;
  private runDone = 2;
  private chatEntries: ChatEntry[] = [
    {
      id: 1,
      ts: Date.now(),
      turn: 'welcome',
      role: 'assistant' as const,
      text: 'Tell me what you would like to learn from these calls.',
    },
  ];
  async call<M extends Method>(method: M, params: ParamsOf<M>): Promise<ResultOf<M>> {
    this.calls.push({ method, params });
    const custom = this.responses.get(method);
    if (custom) return (await custom(params)) as ResultOf<M>;
    if (method === 'projects/list') {
      if (firstRunActive) throw 'RPC error -32002: Setup required';
      return projects.map((project) => ({
        ...project,
        activeJob:
          jobs.find(
            (job) =>
              job.project === project.project &&
              !['done', 'done_with_issues', 'failed', 'cancelled'].includes(job.state),
          ) ?? null,
        lastJob: jobs.find((job) => job.project === project.project) ?? null,
      })) as ResultOf<M>;
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
    if (method === 'allowance/status') return allowanceFixture as ResultOf<M>;
    if (method === 'chat/history') return { entries: this.chatEntries, more: false } as ResultOf<M>;
    if (method === 'ids/attach') {
      const value = String((params as { text: string }).text);
      const raw = value.split(/[\s,]+/).filter(Boolean).length;
      if (!raw) throw new EngineError(-32602, 'No ids', 'no_ids');
      if (raw > 50000) throw new EngineError(-32602, 'Too many ids', 'too_large');
      const distinct = new Set(value.split(/[\s,]+/).filter(Boolean)).size;
      return {
        name: 'ids-3.txt',
        raw,
        distinct,
        duplicates: raw - distinct,
        malformed: 0,
      } as ResultOf<M>;
    }
    if (method === 'chat/send') {
      const text = (params as { text: string }).text;
      const turn = `turn-${Date.now()}`;
      const entryId = this.chatEntries.length + 1;
      this.chatEntries.push({
        id: entryId,
        ts: Date.now(),
        turn,
        role: 'user',
        text,
      });
      const answer =
        'I checked the read-only data. The plan below keeps the transcripts masked and starts with a small sample so you can verify the answers.';
      return new Promise((resolve) => {
        const emitEvent = (event: Record<string, unknown>) =>
          this.emit({
            jsonrpc: '2.0',
            method: 'chat/event',
            params: {
              project: (params as { project: string }).project,
              event: { ...event, turn, entryId },
            },
          });
        emitEvent({ kind: 'system', data: { subtype: 'init' } });
        const chunks = [answer.slice(0, 45), answer.slice(45, 100)];
        chunks.forEach((delta, index) =>
          setTimeout(
            () => emitEvent({ kind: 'assistant_delta', data: { delta: { text: delta } } }),
            index * 500,
          ),
        );
        const tools = [
          ['bq_query', 'tool-1'],
          ['bq_dry_run', 'tool-2'],
          ['propose_job', 'tool-3'],
        ];
        tools.forEach(([name, id], index) =>
          setTimeout(
            () => {
              emitEvent({
                kind: 'tool_use',
                data: {
                  message: {
                    content: [{ type: 'tool_use', id, name: `mcp__bq__${name}`, input: {} }],
                  },
                },
              });
              setTimeout(
                () =>
                  emitEvent({
                    kind: 'tool_result',
                    data: {
                      message: { content: [{ type: 'tool_result', tool_use_id: id, content: [] }] },
                    },
                  }),
                100,
              );
            },
            150 + index * 400,
          ),
        );
        setTimeout(
          () =>
            emitEvent({ kind: 'assistant_delta', data: { delta: { text: answer.slice(100) } } }),
          700,
        );
        const finalText = answer;
        setTimeout(() => {
          this.chatEntries.push({
            id: this.chatEntries.length + 1,
            ts: Date.now(),
            turn,
            role: 'assistant',
            text: finalText,
          });
          emitEvent({
            kind: 'assistant',
            data: { message: { content: [{ type: 'text', text: finalText }] } },
          });
          emitEvent({ kind: 'result', data: { result: finalText } });
          this.emit({
            jsonrpc: '2.0',
            method: 'chat/event',
            params: {
              project: (params as { project: string }).project,
              event: { status: 'completed' },
            },
          });
          resolve({
            status: 'completed',
            project: (params as { project: string }).project,
          } as ResultOf<M>);
        }, 1000);
      }) as Promise<ResultOf<M>>;
    }
    if (method === 'chat/stop')
      return { status: 'stopped', project: (params as { project: string }).project } as ResultOf<M>;
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
    if (method === 'files/list') return files.map((file) => ({ ...file })) as ResultOf<M>;
    if (method === 'job/status')
      return (
        jobs.find((j) => j.job === (params as { job: string }).job) ?? (jobs[0] as ResultOf<M>)
      );
    if (method === 'job/plan') {
      const requestedJob = (params as { job: string }).job;
      return {
        ...(requestedJob === 'synthetic-job-2' ? planWithEstimate : plan),
        job: requestedJob,
      } as ResultOf<M>;
    }
    if (method === 'job/outputs')
      return {
        ...outputsFixture,
        job: (params as { job: string }).job,
      } as JobOutputs as ResultOf<M>;
    if (method === 'job/rebuildRules') {
      const result: RebuildResult = {
        job: (params as { job: string }).job,
        path: 'output/results.v2.rebuild1.xlsx',
        rebuild: 1,
      };
      return result as ResultOf<M>;
    }
    if (method === 'job/sample')
      return { ...sample, job: (params as { job: string }).job } as ResultOf<M>;
    if (method === 'job/approvePlan') {
      if ((params as { expectedSpecVersion: number }).expectedSpecVersion !== plan.specVersion)
        throw new EngineError(-32001, 'The plan changed', 'stale_spec_version');
      plan.approved = true;
      const job = jobs.find((item) => item.job === (params as { job: string }).job);
      if (job) {
        job.state = 'running';
        this.emit({
          jsonrpc: '2.0',
          method: 'job/state',
          params: { project: job.project, job: job.job, state: 'running' },
        });
        setTimeout(() => {
          job.state = 'sample_ready';
          this.emit({
            jsonrpc: '2.0',
            method: 'job/state',
            params: { project: job.project, job: job.job, state: 'sample_ready' },
          });
        }, 2000);
      }
      return job as ResultOf<M>;
    }
    if (method === 'job/approveSample') {
      if ((params as { expectedSpecVersion: number }).expectedSpecVersion !== sample.specVersion)
        throw new EngineError(-32001, 'The plan changed', 'stale_spec_version');
      const job = jobs.find((item) => item.job === (params as { job: string }).job);
      if (job) {
        job.approvedAt = Math.floor(Date.now() / 1000);
        job.state = 'sample_approved';
        this.emit({
          jsonrpc: '2.0',
          method: 'job/state',
          params: { project: job.project, job: job.job, state: 'sample_approved' },
        });
      }
      return job as ResultOf<M>;
    }
    if (method === 'job/snapshot') {
      const requested = (params as { job: string }).job;
      const job = jobs.find((item) => item.job === requested);
      const isPaused = job?.state === 'paused';
      if (!isPaused && job?.state === 'running' && !this.runProgressTimer) {
        const advance = () => {
          this.runProgressTimer = undefined;
          const current = jobs.find((item) => item.job === requested);
          if (!current || current.state !== 'running') return;
          this.runDone = Math.min(8, this.runDone + 1);
          this.emit({
            jsonrpc: '2.0',
            method: 'job/progress',
            params: {
              job: requested,
              project: current.project,
              phase: 'full',
              done: this.runDone,
              inFlight: 4,
              retrying: 0,
              failed: 0,
              heldBack: 0,
              remaining: Math.max(0, 8 - this.runDone - 4),
              total: 8,
            },
          });
          if (this.runDone < 8) this.runProgressTimer = setTimeout(advance, 2000);
        };
        this.runProgressTimer = setTimeout(advance, 2000);
      }
      return {
        ...(quotaPause ? pausedQuotaSnapshot : isPaused ? pausedSnapshot : runningSnapshot),
        ...(quotaPause
          ? { state: 'paused', pauseReason: 'usage_limit', resumeAt: 1700000000 }
          : {}),
        job: requested,
        project: job?.project ?? runningSnapshot.project,
        title:
          projects.find((item) => item.project === job?.project)?.title ?? runningSnapshot.title,
      } as ResultOf<M>;
    }
    if (method === 'job/startFull') {
      const job = jobs.find((item) => item.job === (params as { job: string }).job);
      if (job) {
        job.state = 'running';
        this.emit({
          jsonrpc: '2.0',
          method: 'job/state',
          params: { project: job.project, job: job.job, state: 'running' },
        });
      }
      return job as ResultOf<M>;
    }
    if (method === 'job/pause' || method === 'job/resume') {
      const requested = (params as { job: string }).job;
      const job = jobs.find((item) => item.job === requested);
      if (job) {
        job.state = method === 'job/pause' ? 'paused' : 'running';
        if (job.state === 'paused' && this.runProgressTimer) {
          clearTimeout(this.runProgressTimer);
          this.runProgressTimer = undefined;
        }
        this.emit({
          jsonrpc: '2.0',
          method: 'job/state',
          params: { job: requested, project: job.project, state: job.state, step: job.state },
        });
      }
      return job as ResultOf<M>;
    }
    if (method === 'health/ping') return { status: 'ok' } as ResultOf<M>;
    return {} as ResultOf<M>;
  }
  setResponse<M extends Method>(
    method: M,
    fn: (
      params: ParamsOf<M>,
    ) => ResultOf<M> | Partial<ResultOf<M>> | Promise<ResultOf<M> | Partial<ResultOf<M>>>,
  ): void {
    this.responses.set(method, fn as ResponseFn);
  }
  /** Test helper: forget every response set with setResponse, so tests stay isolated. */
  clearResponses(): void {
    this.responses.clear();
  }
  /** Test helper: stop the run-progress simulation and forget recorded calls and responses. */
  resetForTest(): void {
    if (this.runProgressTimer !== undefined) clearTimeout(this.runProgressTimer);
    this.runProgressTimer = undefined;
    this.runDone = 2;
    this.calls = [];
    this.shellCalls = [];
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
