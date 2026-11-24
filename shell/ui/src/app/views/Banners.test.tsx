import { expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/preact';
import running from '../../mock/fixtures/engine/job-snapshot-running.json';
import { mockBridge } from '../../engine/bridge';
import { RunView } from './RunView';

const cases = [
  [
    'usage limit',
    { state: 'paused', pauseReason: 'usage_limit', resumeAt: 1700000000 },
    'Paused: ChatGPT allowance',
  ],
  [
    'asleep then resumed',
    { recent: [{ ts: 1700000000, kind: 'sleep', text: 'Computer went to sleep' }] },
    'computer went to sleep',
  ],
  ['on battery', { pauseReason: 'battery' }, 'On battery'],
  ['stalled', { state: 'stalled' }, 'Reading has stalled'],
  ['paid credits', { state: 'paused', pauseReason: 'spend_control' }, 'avoid paid credits'],
  ['chat off', { state: 'chat_off' }, 'Chat is off'],
] as const;
for (const [name, changes, text] of cases)
  it(`shows the ${name} banner`, async () => {
    mockBridge.resetForTest();
    mockBridge.setResponse('job/snapshot', async () => ({ ...running, ...changes }) as never);
    render(<RunView id="job-one" />);
    // The sleep text also appears in Recent activity, so always look inside the banner itself.
    const banner = await screen.findByRole('status');
    expect(within(banner).getByText(new RegExp(text, 'i'))).toBeInTheDocument();
  });
it('no banner while running normally', async () => {
  mockBridge.resetForTest();
  render(<RunView id="job-one" />);
  const heroText = await screen.findByText(/of 8 leads read/);
  const hero = heroText.closest('.run-hero');
  expect(hero).not.toBeNull();
  expect(within(hero as HTMLElement).getByText('2')).toBeInTheDocument();
  expect(screen.queryByRole('status')).toBeNull();
});
