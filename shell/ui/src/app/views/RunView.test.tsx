import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/preact';
import running from '../../mock/fixtures/engine/job-snapshot-running.json';
import { RunView } from './RunView';
import { mockBridge } from '../../engine/bridge';
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
      counts: {
        ...(running as unknown as JobSnapshot).counts,
        failed: 1,
        failedByReason: { timeout: 1 },
      },
    }),
  );
  render(<RunView id="job-one" />);
  await waitFor(() => fireEvent.click(screen.getByRole('button', { name: /see why/ })));
  expect(screen.getByText('timeout: 1')).toBeInTheDocument();
});
it('pause calls job/pause and resume calls job/resume', async () => {
  render(<RunView id="job-one" />);
  await waitFor(() => fireEvent.click(screen.getByRole('button', { name: 'Pause' })));
  expect(mockBridge.calls.some((call) => call.method === 'job/pause')).toBe(true);
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
  await waitFor(() => expect(screen.getAllByText('Unknown')).toHaveLength(2));
});
