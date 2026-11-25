import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/preact';
import running from '../../mock/fixtures/engine/job-snapshot-running.json';
import { RunView, plainActivity } from './RunView';
import { mockBridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import type { JobSnapshot } from '../../engine/types';

beforeEach(() => {
  // The mock simulates live progress and records calls; start every test from a clean mock.
  mockBridge.resetForTest();
});
afterEach(() => {
  vi.useRealTimers();
});
const snapshot = (changes: Record<string, unknown> = {}) => ({ ...running, ...changes }) as never;

it('renders the recorded running snapshot', async () => {
  render(<RunView id="job-one" />);
  const hero = await waitFor(() => {
    const element = screen.getByText(/of 8 leads read/).closest('.run-hero');
    expect(element).not.toBeNull();
    return element as HTMLElement;
  });
  expect(within(hero).getByText('2')).toBeInTheDocument();
  expect(screen.getByText('Working out the time left…')).toBeInTheDocument();
});
it('eta unknown shows working out the time left', async () => {
  render(<RunView id="job-one" />);
  await waitFor(() => expect(screen.getByText('Working out the time left…')).toBeInTheDocument());
});
it('eta known shows the range and finish time', async () => {
  mockBridge.setResponse('job/snapshot', async () =>
    snapshot({ eta: { low: 3600, high: 7200, seeded: true, known: true } }),
  );
  render(<RunView id="job-one" />);
  await waitFor(() =>
    expect(screen.getByText(/Somewhere between 1 h and 2 h/)).toBeInTheDocument(),
  );
});
it('last activity ticks every second and shows a dash when null', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  render(<RunView id="job-one" />);
  await vi.advanceTimersByTimeAsync(1000);
  await waitFor(() => expect(screen.getByText(/Last activity —/)).toBeInTheDocument());
});
it('progress notification updates counts in place', async () => {
  render(<RunView id="job-one" />);
  const initialHero = await waitFor(() => {
    const element = screen.getByText(/of 8 leads read/).closest('.run-hero');
    expect(element).not.toBeNull();
    return element as HTMLElement;
  });
  expect(within(initialHero).getByText('2')).toBeInTheDocument();
  mockBridge.emit({
    jsonrpc: '2.0',
    method: 'job/progress',
    params: {
      job: 'job-one',
      done: 5,
      inFlight: 1,
      remaining: 2,
      total: 8,
      failed: 0,
      heldBack: 0,
      retrying: 0,
    },
  });
  await waitFor(() => {
    const hero = screen.getByText(/of 8 leads read/).closest('.run-hero');
    expect(hero).not.toBeNull();
    expect(within(hero as HTMLElement).getByText('5')).toBeInTheDocument();
  });
});
it('events arriving before the first snapshot are applied once it loads', async () => {
  let resolveSnapshot!: (value: JobSnapshot) => void;
  mockBridge.setResponse(
    'job/snapshot',
    () => new Promise<JobSnapshot>((resolve) => (resolveSnapshot = resolve)),
  );
  render(<RunView id="job-one" />);
  await waitFor(() =>
    expect(mockBridge.calls.filter((call) => call.method === 'job/snapshot')).toHaveLength(1),
  );
  mockBridge.emit({
    jsonrpc: '2.0',
    method: 'job/progress',
    params: {
      job: 'job-one',
      done: 5,
      inFlight: 1,
      remaining: 2,
      total: 8,
      failed: 0,
      heldBack: 0,
      retrying: 0,
    },
  });
  mockBridge.emit({
    jsonrpc: '2.0',
    method: 'engine/event',
    params: {
      job: 'job-one',
      event: { kind: 'activity', workers: 3, last_activity_ts: 1700000000 },
    },
  });
  resolveSnapshot!(snapshot());
  await waitFor(() => expect(screen.getByText(/of 8 leads read/)).toBeInTheDocument());
  // "5" appears in the hero and in the counts grid; check the hero.
  const hero = screen.getByText(/of 8 leads read/).closest('.run-hero') as HTMLElement;
  expect(within(hero).getByText('5')).toBeInTheDocument();
  expect(screen.getByText(/3 readers working/)).toBeInTheDocument();
});
it('the accounted-for equation uses only real fields and adds up', async () => {
  render(<RunView id="job-one" />);
  await waitFor(() =>
    expect(screen.getByText(/8 = 2 read \+ 4 in progress \+ 2 left/)).toBeInTheDocument(),
  );
  expect(screen.queryByText(/doesn't add up/)).toBeNull();
});
it('failed opens the reasons', async () => {
  mockBridge.setResponse('job/snapshot', async () =>
    snapshot({
      progress: {
        ...(running as unknown as JobSnapshot).progress,
        failed: 1,
      },
      counts: {
        ...(running as unknown as JobSnapshot).counts,
        failed: 1,
        failedByReason: { timeout: 1 },
      },
    }),
  );
  render(<RunView id="job-one" />);
  await waitFor(() => fireEvent.click(screen.getByRole('button', { name: /see why/i })));
  expect(screen.getByText('timeout: 1')).toBeInTheDocument();
});
it('run view leaves job actions to the main header', async () => {
  render(<RunView id="job-one" />);
  await waitFor(() => expect(screen.getByTestId('run-view')).toBeInTheDocument());
  expect(screen.queryByRole('button', { name: 'Pause' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Open folder' })).toBeNull();
});
it('snapshot refetch happens on job/state and at most every 30 seconds', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  render(<RunView id="job-one" />);
  await waitFor(() =>
    expect(mockBridge.calls.filter((call) => call.method === 'job/snapshot')).toHaveLength(1),
  );
  mockBridge.emit({
    jsonrpc: '2.0',
    method: 'job/state',
    params: { job: 'job-one', state: 'running' },
  });
  await waitFor(() =>
    expect(mockBridge.calls.filter((call) => call.method === 'job/snapshot')).toHaveLength(2),
  );
  await vi.advanceTimersByTimeAsync(30000);
  await waitFor(() =>
    expect(mockBridge.calls.filter((call) => call.method === 'job/snapshot')).toHaveLength(3),
  );
});
it('quota tile shows the projection, stop marker and reset time', async () => {
  mockBridge.setResponse('job/snapshot', async () =>
    snapshot({
      quota: {
        ...(running as unknown as JobSnapshot).quota,
        projectedEndPercent: 52,
        codex: {
          ...(running as unknown as JobSnapshot).quota.codex,
          primary: { usedPercent: 28, windowDurationMins: 300, resetsAt: 1700000000 },
          stopAtPercent: 95,
        },
      },
    }),
  );
  render(<RunView id="job-one" />);
  await waitFor(() => expect(screen.getByText('This job ends near 52%')).toBeInTheDocument());
  expect(screen.getByText('Pauses at 95%')).toBeInTheDocument();
});
it('unknown allowances and power show unknown', async () => {
  render(<RunView id="job-one" />);
  await waitFor(() => expect(screen.getAllByText('Unknown')).toHaveLength(1));
});

it('progress with retrying leads adds up', async () => {
  mockBridge.setResponse('job/snapshot', async () =>
    snapshot({
      progress: {
        ...(running as unknown as JobSnapshot).progress,
        done: 2,
        inFlight: 1,
        retrying: 1,
        remaining: 4,
      },
    }),
  );
  render(<RunView id="job-one" />);
  expect(
    await screen.findByText(/8 = 2 read \+ 1 in progress \+ 1 retrying \+ 4 left/),
  ).toBeInTheDocument();
  expect(screen.queryByText(/doesn.t add up/)).toBeNull();
});

it('unusable leads add up in the second line', async () => {
  mockBridge.setResponse('job/snapshot', async () =>
    snapshot({
      progress: {
        ...(running as unknown as JobSnapshot).progress,
        done: 2,
        inFlight: 2,
        remaining: 0,
        total: 4,
      },
      counts: {
        ...(running as unknown as JobSnapshot).counts,
        selected: 8,
        usable: 4,
        noTranscript: 1,
        transcriptionFailed: 1,
        tooShort: 1,
        notFound: 1,
      },
    }),
  );
  render(<RunView id="job-one" />);
  expect(
    await screen.findByText(
      /8 selected = 4 usable \+ 1 no transcript \+ 1 transcription failed \+ 1 too short \+ 1 not found/,
    ),
  ).toBeInTheDocument();
});

it('sample phase counts sample leads', async () => {
  mockBridge.setResponse('job/snapshot', async () =>
    snapshot({
      progress: {
        ...(running as unknown as JobSnapshot).progress,
        phase: 'sample',
        done: 1,
        inFlight: 1,
        remaining: 0,
        total: 2,
      },
      counts: { ...(running as unknown as JobSnapshot).counts, selected: 2, usable: 2 },
    }),
  );
  render(<RunView id="job-one" />);
  expect(await screen.findByText('of 2 sample leads read')).toBeInTheDocument();
});

it('switching jobs never shows the other job', async () => {
  let resolveFirst!: (value: JobSnapshot) => void;
  mockBridge.setResponse('job/snapshot', (params) =>
    params.job === 'job-one'
      ? new Promise<JobSnapshot>((resolve) => (resolveFirst = resolve))
      : Promise.resolve(
          snapshot({
            job: 'job-two',
            progress: { ...(running as unknown as JobSnapshot).progress, total: 100, done: 100 },
          }),
        ),
  );
  const view = render(<RunView id="job-one" />);
  view.rerender(<RunView id="job-two" />);
  expect(await screen.findByText(/of 100 leads read/)).toBeInTheDocument();
  resolveFirst?.(
    snapshot({ progress: { ...(running as unknown as JobSnapshot).progress, done: 5 } }),
  );
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(screen.queryByText(/of 8 leads read/)).toBeNull();
});

it('method not found shows not available yet', async () => {
  mockBridge.setResponse('job/snapshot', async () => {
    throw new EngineError(-32601, 'Method not found');
  });
  render(<RunView id="job-one" />);
  expect(await screen.findByText('Live progress isn’t available yet')).toBeInTheDocument();
});

it('numbers are rounded', async () => {
  mockBridge.setResponse('job/snapshot', async () =>
    snapshot({
      eta: { low: 7500, high: 7500, seeded: true, known: true },
      activity: { ...(running as unknown as JobSnapshot).activity, throughputPerMin: 1.26 },
    }),
  );
  render(<RunView id="job-one" />);
  const hero = (await screen.findByText(/Somewhere between 2 h 5 min/)).closest(
    '.run-hero',
  ) as HTMLElement;
  expect(within(hero).getAllByText(/2 h 5 min/).length).toBeGreaterThan(0);
  expect(screen.getByText(/1\.3 leads a minute/)).toBeInTheDocument();
});

it('activity lines use plain words for the engine step names', () => {
  expect(plainActivity('State changed from sample_approved to full_running.')).toBe(
    'State changed from sample approved to reading every lead.',
  );
  expect(plainActivity('State changed from full_running to paused.')).toBe(
    'State changed from reading every lead to paused.',
  );
  expect(plainActivity('Lead 1002 failed: quota_interrupt')).toBe(
    'Lead 1002 failed: quota interrupt',
  );
});
