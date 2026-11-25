import { beforeEach, describe, expect, it } from 'vitest';
import running from '../mock/fixtures/engine/job-snapshot-running.json';
import { initialJobLive, reduce } from './jobLive';
import type { JobSnapshot, Notification } from '../engine/types';
import { mockBridge } from '../engine/mockBridge';

beforeEach(() => {
  mockBridge.resetForTest();
});
const snapshot = (changes: Partial<JobSnapshot> = {}) =>
  ({ ...running, ...changes }) as JobSnapshot;
const progress = (done: number): Notification => ({
  jsonrpc: '2.0',
  method: 'job/progress',
  params: { job: 'job-one', done },
});
const activity = (job = 'job-one'): Notification => ({
  jsonrpc: '2.0',
  method: 'engine/event',
  params: { job, kind: 'activity', workers: 3 },
});

describe('job live reducer', () => {
  it('buffers early events and applies them once', () => {
    let state = initialJobLive('job-one');
    state = reduce(state, { type: 'notification', notification: progress(5) });
    state = reduce(state, { type: 'snapshot/requested', sequence: 1 });
    state = reduce(state, { type: 'snapshot/loaded', sequence: 1, snapshot: snapshot() });
    expect(state.snapshot?.progress.done).toBe(5);
    state = reduce(state, {
      type: 'snapshot/loaded',
      sequence: 2,
      snapshot: snapshot({ progress: { ...snapshot().progress, done: 2 } }),
    });
    expect(state.snapshot?.progress.done).toBe(2);
  });
  it('older snapshot never overwrites a newer one', () => {
    let state = reduce(initialJobLive('job-one'), { type: 'snapshot/requested', sequence: 2 });
    state = reduce(state, {
      type: 'snapshot/loaded',
      sequence: 2,
      snapshot: snapshot({ progress: { ...snapshot().progress, done: 7 } }),
    });
    state = reduce(state, {
      type: 'snapshot/loaded',
      sequence: 1,
      snapshot: snapshot({ progress: { ...snapshot().progress, done: 1 } }),
    });
    expect(state.snapshot?.progress.done).toBe(7);
  });
  it('events for another job are ignored', () => {
    let state = reduce(initialJobLive('job-one'), { type: 'snapshot/requested', sequence: 1 });
    state = reduce(state, { type: 'snapshot/loaded', sequence: 1, snapshot: snapshot() });
    expect(reduce(state, { type: 'notification', notification: activity('job-two') })).toEqual(
      state,
    );
  });
  it('resume clears the pause', () => {
    let state = reduce(initialJobLive('job-one'), { type: 'snapshot/requested', sequence: 1 });
    state = reduce(state, {
      type: 'snapshot/loaded',
      sequence: 1,
      snapshot: snapshot({ state: 'paused', pauseReason: 'quota', resumeAt: 1 }),
    });
    state = reduce(state, {
      type: 'notification',
      notification: {
        jsonrpc: '2.0',
        method: 'job/state',
        params: { job: 'job-one', state: 'running' },
      },
    });
    expect(state.snapshot?.pauseReason).toBeNull();
    expect(state.snapshot?.resumeAt).toBeNull();
  });
  it('stall clears on the next heartbeat only when the state says so', () => {
    let state = reduce(initialJobLive('job-one'), { type: 'snapshot/requested', sequence: 1 });
    state = reduce(state, {
      type: 'snapshot/loaded',
      sequence: 1,
      snapshot: snapshot({ state: 'stalled' }),
    });
    state = reduce(state, { type: 'tick', state: 'stalled' });
    expect(state.stalled).toBe(true);
    expect(reduce(state, { type: 'tick', state: 'running' }).stalled).toBe(false);
  });
  it('switching jobs keeps nothing', () => {
    const state = reduce(initialJobLive('job-one'), { type: 'reset', job: 'job-two' });
    expect(state.snapshot).toBeNull();
    expect(state.job).toBe('job-two');
  });
  it('failed reload keeps the last good data', () => {
    let state = reduce(initialJobLive('job-one'), { type: 'snapshot/requested', sequence: 1 });
    state = reduce(state, { type: 'snapshot/loaded', sequence: 1, snapshot: snapshot() });
    state = reduce(state, { type: 'snapshot/requested', sequence: 2 });
    state = reduce(state, { type: 'snapshot/failed', sequence: 2, error: 'timeout' });
    expect(state.snapshot?.job).toBe('job-one');
    expect(state.error).toBe("Couldn't refresh. Retry");
  });
});
