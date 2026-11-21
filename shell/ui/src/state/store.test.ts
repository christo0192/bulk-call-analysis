import { describe, expect, it, beforeEach, vi } from 'vitest';
import {
  phaseFor,
  jobsByProject,
  reduceNotification,
  load,
  engineStatus,
  setupRequired,
} from './store';
import type { Notification, Project } from '../engine/types';
import { mockBridge } from '../engine/bridge';
describe('phase mapping', () => {
  beforeEach(() => {
    jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'planned' }] };
  });
  it.each(['planned', 'plan_approved', undefined])('%s is setup', (s) =>
    expect(phaseFor(s as never)).toBe('setup'),
  );
  it('sample', () => expect(phaseFor('sample_ready')).toBe('sample'));
  it.each(['running', 'paused', 'stalled'])('%s is run', (s) =>
    expect(phaseFor(s as never)).toBe('run'),
  );
  it.each(['done', 'done_with_issues'])('%s is done', (s) =>
    expect(phaseFor(s as never)).toBe('done'),
  );
});
it('maps a missing job to setup', () => expect(phaseFor(undefined)).toBe('setup'));
it('maps plan approval to setup', () => expect(phaseFor('plan_approved')).toBe('setup'));
describe('reducer', () => {
  it('updates state', () => {
    jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'planned' }] };
    reduceNotification({
      jsonrpc: '2.0',
      method: 'job/state',
      params: { job: 'j', state: 'running' },
    });
    expect(jobsByProject.value.p?.[0]?.state).toBe('running');
  });
  it('updates progress', () => {
    jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'running' }] };
    reduceNotification({
      jsonrpc: '2.0',
      method: 'job/progress',
      params: { job: 'j', done: 4, total: 9, remaining: 5 },
    });
    expect(jobsByProject.value.p?.[0]?.done).toBe(4);
  });
  it('progress accepts remaining as a string or a number', () => {
    jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'running' }] };
    reduceNotification({
      jsonrpc: '2.0',
      method: 'job/progress',
      params: { job: 'j', remaining: '5' },
    });
    expect(jobsByProject.value.p?.[0]?.remaining).toBe(5);
    reduceNotification({
      jsonrpc: '2.0',
      method: 'job/progress',
      params: { job: 'j', remaining: 3 },
    });
    expect(jobsByProject.value.p?.[0]?.remaining).toBe(3);
  });
  it('ignores unknown', () => {
    jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'planned' }] };
    reduceNotification({ jsonrpc: '2.0', method: 'engine/event', params: { kind: 'x' } });
    expect(jobsByProject.value.p?.[0]?.state).toBe('planned');
  });
});
it('ignores a state event without a job', () => {
  jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'planned' }] };
  reduceNotification({ jsonrpc: '2.0', method: 'job/state', params: { state: 'running' } });
  expect(jobsByProject.value.p?.[0]?.state).toBe('planned');
});
it('ignores progress for another job', () => {
  jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'running', done: 1 }] };
  reduceNotification({ jsonrpc: '2.0', method: 'job/progress', params: { job: 'other', done: 4 } });
  expect(jobsByProject.value.p?.[0]?.done).toBe(1);
});
it('registers listeners before projects load completes', async () => {
  let resolveList: ((value: Project[]) => void) | undefined;
  mockBridge.setResponse(
    'projects/list',
    () =>
      new Promise<Project[]>((resolve) => {
        resolveList = resolve;
      }),
  );
  jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'planned' }] };
  mockBridge.calls = [];
  const pending = load();
  await vi.waitFor(() =>
    expect(mockBridge.calls.some((call) => call.method === 'projects/list')).toBe(true),
  );
  mockBridge.emit({
    jsonrpc: '2.0',
    method: 'job/state',
    params: { job: 'j', state: 'running' },
  });
  expect(jobsByProject.value.p?.[0]?.state).toBe('running');
  resolveList?.([{ project: 'p', title: 'Project' }]);
  await pending;
});
it('does not register duplicate listeners on a second load', async () => {
  jobsByProject.value = { p: [{ job: 'j', project: 'p', state: 'planned' }] };
  mockBridge.setResponse('projects/list', async () => []);
  await load();
  await load();
  mockBridge.emit({
    jsonrpc: '2.0',
    method: 'job/state',
    params: { job: 'j', state: 'running' },
  });
  expect(jobsByProject.value.p?.[0]?.state).toBe('running');
});
it('marks setup required without failing the engine', async () => {
  setupRequired.value = false;
  mockBridge.setResponse('projects/list', async () => {
    throw { code: -32002, message: 'Setup required' };
  });
  await load();
  expect(setupRequired.value).toBe(true);
  expect(engineStatus.value).not.toBe('failed');
});

it('setup required in text form shows the checklist without the unavailable banner', async () => {
  setupRequired.value = false;
  mockBridge.setResponse('projects/list', async () => {
    throw 'RPC error -32002: Setup required';
  });
  await load();
  expect(setupRequired.value).toBe(true);
  expect(engineStatus.value).not.toBe('failed');
});

it('load treats method-not-found as not available and keeps the engine ready', async () => {
  mockBridge.setResponse('projects/list', async () => []);
  mockBridge.setResponse('allowance/status', async () => {
    throw { code: -32601, message: 'Method not found' };
  });
  await load();
  expect(engineStatus.value).toBe('ready');
});

it('engine ready after a failed load triggers exactly one reload', async () => {
  let attempts = 0;
  mockBridge.setResponse('projects/list', async () => {
    attempts += 1;
    if (attempts === 1) throw new Error('offline');
    return [];
  });
  await load();
  expect(engineStatus.value).toBe('failed');
  mockBridge.emitStatus('ready');
  await vi.waitFor(() => expect(attempts).toBe(2));
  mockBridge.emitStatus('ready');
  await Promise.resolve();
  expect(attempts).toBe(2);
});

it.each([
  ['draft', 'setup'],
  ['planned', 'setup'],
  ['plan_proposed', 'setup'],
  ['plan_approved', 'setup'],
  ['running', 'run'],
  ['pulling', 'run'],
  ['masking_sample', 'run'],
  ['sample_running', 'run'],
  ['full_running', 'run'],
  ['quote_check', 'run'],
  ['building_output', 'run'],
  ['verifying', 'run'],
  ['sample_ready', 'sample'],
  ['sample_approved', 'sample'],
  ['paused', 'run'],
  ['stalled', 'run'],
  ['done', 'done'],
  ['done_with_issues', 'done'],
  ['failed', 'setup'],
  ['cancelled', 'setup'],
  ['unknown', 'setup'],
])('every engine state maps to a label and a phase: %s', (state, expected) => {
  expect(phaseFor(state)).toBe(expected);
});
