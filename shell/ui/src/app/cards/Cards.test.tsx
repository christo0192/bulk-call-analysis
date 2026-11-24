import { act, fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { beforeEach, expect, it, vi } from 'vitest';
import { bytes } from './Cards';
import { PlanCard, SampleCard, StartCard } from './Cards';
import { formatCount } from '../format';
import { mockBridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import { plan, sample } from '../../mock/fixtures/data';
import { adaptPlan } from '../../engine/adapters';

beforeEach(() => {
  mockBridge.calls = [];
  mockBridge.clearResponses();
  mockBridge.setResponse('job/plan', async (params) => ({
    ...plan,
    job: (params as { job: string }).job,
  }));
  mockBridge.setResponse('job/sample', async (params) => ({
    ...sample,
    job: (params as { job: string }).job,
  }));
  mockBridge.setResponse('job/approvePlan', async () => ({
    job: 'job-one',
    project: 'p1',
    state: 'plan_approved',
  }));
  mockBridge.setResponse('job/approveSample', async () => ({
    job: 'job-one',
    project: 'p1',
    state: 'sample_approved',
  }));
  mockBridge.setResponse('job/startFull', async () => ({
    job: 'job-one',
    project: 'p1',
    state: 'running',
  }));
  mockBridge.setResponse('job/snapshot', async () => ({
    job: 'job-one',
    project: 'p1',
    state: 'sample_approved',
    approvedAt: 1_700_000_000,
  }));
});

it('formats one byte as a minimum kilobyte', () => expect(bytes(1)).toBe('0.0 KB'));
it('formats kilobytes with one decimal', () => expect(bytes(2048)).toBe('2.0 KB'));
it('formats megabytes with one decimal', () => expect(bytes(2 * 1024 ** 2)).toBe('2.0 MB'));
it('formats gigabytes with one decimal', () => expect(bytes(3 * 1024 ** 3)).toBe('3.0 GB'));
it('formats zero as zero', () => expect(formatCount(0, 'en-US')).toBe('0'));

it('plan card renders with a null estimate', async () => {
  render(<PlanCard job="job-one" />);
  await screen.findByText(plan.question);
  expect(screen.queryByText(/Sample of/)).toBeNull();
});

it('start card renders with a null estimate and no approvedAt', async () => {
  mockBridge.setResponse('job/snapshot', async () => ({
    job: 'job-one',
    project: 'p1',
    state: 'sample_approved',
  }));
  render(<StartCard job="job-one" project="p1" />);
  expect(await screen.findByText("We'll know after a few minutes of reading")).toBeInTheDocument();
  expect(screen.getByText(/sample approved/)).toHaveTextContent('unknown');
});

it('plan counts accept snake_case and camelCase', () => {
  const base = { ...plan, estimate: null };
  expect(
    adaptPlan({
      ...base,
      counts: {
        selected: 10,
        usable: 11,
        capped: 12,
        no_transcript: 2,
        transcription_failed: 3,
        too_short: 4,
        not_found: 5,
      },
    }).counts,
  ).toMatchObject({ noTranscript: 2, transcriptionFailed: 3, tooShort: 4, notFound: 5 });
  expect(
    adaptPlan({
      ...base,
      counts: { ...base.counts, noTranscript: 6, transcriptionFailed: 7, tooShort: 8, notFound: 9 },
    }).counts,
  ).toMatchObject({ noTranscript: 6, transcriptionFailed: 7, tooShort: 8, notFound: 9 });
});
it('formats thousands with separators', () => expect(formatCount(11058, 'en-US')).toBe('11,058'));
it('formats millions with en-US separators', () =>
  expect(formatCount(1000000, 'en-US')).toBe('1,000,000'));
it('formats millions with en-IN separators', () =>
  expect(formatCount(1000000, 'en-IN')).toBe('10,00,000'));

it("switching jobs never shows the previous job's plan", async () => {
  const view = render(<PlanCard job="job-one" />);
  await screen.findByText(plan.question);
  view.rerender(<PlanCard job="job-two" />);
  expect(screen.queryByText(plan.question)).toBeNull();
  await screen.findByText(plan.question);
  expect(mockBridge.calls.filter((call) => call.method === 'job/plan')).toHaveLength(2);
});

it("approve sends the displayed plan's job and version", async () => {
  render(<PlanCard job="job-two" />);
  fireEvent.click(await screen.findByRole('button', { name: 'Approve plan' }));
  await waitFor(() =>
    expect(mockBridge.calls).toContainEqual({
      method: 'job/approvePlan',
      params: { job: 'job-two', expectedSpecVersion: 1 },
    }),
  );
});

it('change something focuses the composer with a prefix', async () => {
  let prefix = '';
  render(
    <PlanCard
      job="job-one"
      onFocus={() => {
        prefix = 'Change: ';
      }}
    />,
  );
  fireEvent.click(await screen.findByRole('button', { name: 'Change something' }));
  expect(prefix).toBe('Change: ');
});

it('approve stays disabled after success until the state changes', async () => {
  vi.useFakeTimers();
  try {
    render(<PlanCard job="job-one" />);
    const button = await screen.findByRole('button', { name: 'Approve plan' });
    fireEvent.click(button);
    await waitFor(() => expect(button).toBeDisabled());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9_999);
    });
    expect(button).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(button).not.toBeDisabled();
  } finally {
    vi.useRealTimers();
  }
});

it('stale plan message appears once and the sample card reloads on stale', async () => {
  let first = true;
  mockBridge.setResponse('job/approvePlan', async () => {
    if (first) {
      first = false;
      throw new EngineError(-32001, 'The plan changed', 'stale_spec_version');
    }
    return { job: 'job-one', project: 'p1', state: 'plan_approved' };
  });
  render(<PlanCard job="job-one" />);
  fireEvent.click(await screen.findByRole('button', { name: 'Approve plan' }));
  expect(await screen.findByText('The plan changed. Check the new version.')).toBeInTheDocument();
  expect(screen.getAllByText('The plan changed. Check the new version.')).toHaveLength(1);
  expect(mockBridge.calls.filter((call) => call.method === 'job/plan')).toHaveLength(2);
});

it("start card uses the job's approval time and the engine's stop percent", async () => {
  render(
    <StartCard
      job="job-one"
      project="p1"
      stopAtPercent={88}
      allowance={{ codex: { windows: [], stopAtPercent: 91 }, claude: null }}
    />,
  );
  const approvalTime = new Date(1_700_000_000 * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });
  expect(await screen.findByText(/sample approved/)).toHaveTextContent(approvalTime);
  expect(screen.getByText(/New leads stop at 91%/)).toBeInTheDocument();
});

it('not now collapses the start card and it can be reopened', async () => {
  render(<StartCard job="job-one" project="p1" />);
  fireEvent.click(await screen.findByRole('button', { name: 'Not now' }));
  fireEvent.click(screen.getByRole('button', { name: 'Start the full run' }));
  expect(screen.getByRole('button', { name: 'Not now' })).toBeInTheDocument();
});
