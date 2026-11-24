import { beforeEach, expect, it } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/preact';
import running from '../../mock/fixtures/engine/job-snapshot-running.json';
import { mockBridge } from '../../engine/bridge';
import { RunView } from './RunView';

beforeEach(() => mockBridge.resetForTest());
const snapshot = (changes: Record<string, unknown>) => ({ ...running, ...changes }) as never;
const reasons = [
  'quota',
  'user',
  'sleep',
  'credit',
  'auth',
  'model_unavailable',
  'error_storm',
  'network',
  'spend_control',
  'power',
];

it('every real pause reason has its banner', async () => {
  for (const reason of reasons) {
    mockBridge.resetForTest();
    mockBridge.setResponse('job/snapshot', async () =>
      snapshot({
        state: reason === 'user' ? 'paused' : 'paused',
        pauseReason: reason,
        resumeAt: reason === 'quota' ? 1795523054 : null,
      }),
    );
    render(<RunView id="job-one" />);
    if (reason === 'user') expect(screen.queryByRole('status')).toBeNull();
    else expect(await screen.findByRole('status')).toBeInTheDocument();
    cleanup();
  }
});
it('quota without a reset time asks for resume', async () => {
  mockBridge.setResponse('job/snapshot', async () =>
    snapshot({ state: 'paused', pauseReason: 'quota', resumeAt: null }),
  );
  render(<RunView id="job-one" />);
  expect(await screen.findByText('Press Resume when your allowance is back.')).toBeInTheDocument();
});
it('credit buttons call the engine', async () => {
  mockBridge.setResponse('job/snapshot', async () =>
    snapshot({ state: 'paused', pauseReason: 'credit', resumeAt: null }),
  );
  render(<RunView id="job-one" />);
  const banner = await screen.findByRole('status');
  const buttons = within(banner).getAllByRole('button');
  buttons[0]?.click();
  expect(mockBridge.calls.some((call) => call.method === 'job/resume')).toBe(true);
});
it('stalled offers no pause', async () => {
  mockBridge.setResponse('job/snapshot', async () => snapshot({ state: 'stalled' }));
  render(<RunView id="job-one" />);
  const banner = await screen.findByRole('status');
  expect(within(banner).queryByRole('button', { name: 'Pause' })).toBeNull();
});
it('failed job shows the engine reason', async () => {
  mockBridge.setResponse('job/snapshot', async () =>
    snapshot({ state: 'failed', failure: { text: 'Reader stopped: synthetic failure' } }),
  );
  render(<RunView id="job-one" />);
  expect(await screen.findByText('Reader stopped: synthetic failure')).toBeInTheDocument();
});
it('banner goes away after resume', async () => {
  let paused = true;
  mockBridge.setResponse('job/snapshot', async () =>
    snapshot(
      paused
        ? { state: 'paused', pauseReason: 'quota', resumeAt: null }
        : { state: 'running', pauseReason: null, resumeAt: null },
    ),
  );
  render(<RunView id="job-one" />);
  expect(await screen.findByRole('status')).toBeInTheDocument();
  paused = false;
  mockBridge.emit({
    jsonrpc: '2.0',
    method: 'job/state',
    params: { job: 'job-one', state: 'running' },
  });
  expect(await screen.findByText(/of 8 leads read/)).toBeInTheDocument();
});
