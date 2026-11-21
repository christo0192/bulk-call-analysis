import { fireEvent, render, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { SetupChecklist } from './SetupChecklist';
import { mockBridge } from '../../engine/bridge';
import type { AuthStatus, SetupItem } from '../../engine/types';
import { setupItems, setupRequired, settingsOpen } from '../../state/store';

const auth: AuthStatus = {
  claude: { loggedIn: false },
  codex: { loggedIn: true, planType: 'Plus', usagePercent: 12 },
};
const complete: SetupItem[] = [
  'uv_python',
  'venv',
  'spacy',
  'connector',
  'key',
  'claude_copy',
  'codex_copy',
].map((name) => ({ name, status: 'ok' }));

function items(overrides: SetupItem[] = []): SetupItem[] {
  const base: SetupItem[] = [
    ...complete,
    { name: 'claude_login', status: 'ok' },
    { name: 'chatgpt_login', status: 'ok' },
    { name: 'training_off', status: 'manual_done' },
    { name: 'extra_usage_off', status: 'manual_done' },
  ];
  return base.map((item) => overrides.find((override) => override.name === item.name) ?? item);
}
function renderChecklist(next = items()) {
  setupItems.value = next;
  setupRequired.value = true;
  settingsOpen.value = false;
  return render(<SetupChecklist />);
}
function setChecks(fn: () => SetupItem[]) {
  mockBridge.setResponse('setup/runChecks', async () => ({ items: fn() }));
  mockBridge.setResponse('auth/status', async () => auth);
}

beforeEach(() => {
  vi.useRealTimers();
  mockBridge.calls = [];
  mockBridge.setResponse('auth/status', async () => auth);
});
afterEach(() => vi.useRealTimers());

it('shows the checklist when projects/list says setup is required', async () => {
  mockBridge.setResponse('projects/list', async () => {
    throw { code: -32002, message: 'Setup required' };
  });
  mockBridge.setResponse('setup/checklist', async () =>
    items([{ name: 'claude_login', status: 'pending' }]),
  );
  render(<App />);
  expect(await screen.findByText('Set up this computer')).toBeInTheDocument();
});

it('shows the checklist when any item is pending', () => {
  renderChecklist(items([{ name: 'claude_login', status: 'pending' }]));
  expect(screen.getByText('Sign in to Claude')).toBeInTheDocument();
});

it('App shows the checklist when a loaded item is pending', async () => {
  mockBridge.setResponse('projects/list', async () => []);
  mockBridge.setResponse('setup/checklist', async () =>
    items([{ name: 'claude_login', status: 'pending' }]),
  );
  render(<App />);
  expect(await screen.findByText('Set up this computer')).toBeInTheDocument();
});

it('groups engine, connector and program items into one row each', () => {
  const { container } = renderChecklist(items([{ name: 'venv', status: 'pending' }]));
  expect(container.querySelectorAll('.check-row')).toHaveLength(8);
  expect(screen.getByText('App engine installed')).toBeInTheDocument();
  expect(screen.getByText('BigQuery connector found')).toBeInTheDocument();
});

it('sign in starts the official flow and polls runChecks until ok', async () => {
  vi.useFakeTimers();
  let checks = 0;
  const pending = items([{ name: 'claude_login', status: 'pending' }]);
  setChecks(() => {
    checks += 1;
    return checks >= 2 ? items() : pending;
  });
  renderChecklist(pending);
  const runChecks = () =>
    mockBridge.calls.filter((call) => call.method === 'setup/runChecks').length;
  // The checklist runs the checks once when it opens; count only the polls after Sign in.
  await vi.advanceTimersByTimeAsync(0);
  const beforeSignIn = runChecks();
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
  await vi.advanceTimersByTimeAsync(15000);
  expect(screen.getByText('Signed in to ChatGPT')).toBeInTheDocument();
  const afterSignIn = runChecks();
  expect(afterSignIn - beforeSignIn).toBeGreaterThanOrEqual(1);
  // Polling stops once the row is ok.
  await vi.advanceTimersByTimeAsync(15000);
  expect(runChecks()).toBe(afterSignIn);
});

it('login rows are ok when auth/status says signed in even if the probe is missing', async () => {
  const failed = items([
    { name: 'claude_login', status: 'failed', detail: 'probe not supplied' },
    { name: 'chatgpt_login', status: 'failed', detail: 'probe not supplied' },
  ]);
  mockBridge.setResponse('setup/runChecks', async () => ({ items: failed }));
  mockBridge.setResponse('auth/status', async () => ({
    claude: { loggedIn: true },
    codex: { loggedIn: true },
  }));
  renderChecklist(failed);
  await waitFor(() => expect(screen.getByText('Signed in to ChatGPT')).toBeInTheDocument());
  expect(screen.queryAllByText('Not signed in yet')).toHaveLength(0);
});

it('continue is enabled with the real-shaped checklist and signed-in auth', async () => {
  const failed = items([
    { name: 'claude_login', status: 'failed', detail: 'probe not supplied' },
    { name: 'chatgpt_login', status: 'failed', detail: 'probe not supplied' },
  ]);
  mockBridge.setResponse('setup/runChecks', async () => ({ items: failed }));
  mockBridge.setResponse('auth/status', async () => ({
    claude: { loggedIn: true },
    codex: { loggedIn: true },
  }));
  renderChecklist(failed);
  await waitFor(() => expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled());
});

it('a missing login probe shows Not signed in yet, never the raw detail', async () => {
  const failed = items([{ name: 'claude_login', status: 'failed', detail: 'probe not supplied' }]);
  mockBridge.setResponse('setup/runChecks', async () => ({ items: failed }));
  renderChecklist(failed);
  await waitFor(() => expect(screen.getByText('Not signed in yet')).toBeInTheDocument());
  expect(screen.queryByText('probe not supplied')).toBeNull();
});

it("I've checked updates the row from the {item, status} reply", async () => {
  renderChecklist(items([{ name: 'training_off', status: 'manual_pending' }]));
  mockBridge.setResponse('setup/markManual', async () => ({
    item: 'training_off',
    status: 'manual_done',
  }));
  fireEvent.click(screen.getByRole('button', { name: "I've checked" }));
  const row = screen
    .getByText('Model training is off in both accounts')
    .closest('section') as HTMLElement;
  await waitFor(() => expect(within(row).getByText('✓')).toBeInTheDocument());
});

it('the checklist runs the checks when it opens', async () => {
  renderChecklist();
  await waitFor(() =>
    expect(mockBridge.calls.filter((call) => call.method === 'setup/runChecks')).toHaveLength(1),
  );
});

it('sign in stops polling after five minutes', async () => {
  vi.useFakeTimers();
  const pending = items([{ name: 'claude_login', status: 'pending' }]);
  setChecks(() => pending);
  renderChecklist(pending);
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
  await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
  const count = mockBridge.calls.filter((call) => call.method === 'setup/runChecks').length;
  await vi.advanceTimersByTimeAsync(10000);
  expect(mockBridge.calls.filter((call) => call.method === 'setup/runChecks')).toHaveLength(count);
  expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument();
});

it('check again re-runs the checks', async () => {
  const failed = items([{ name: 'claude_login', status: 'failed', detail: 'Login expired' }]);
  setChecks(() => items());
  renderChecklist(failed);
  fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
  await waitFor(() =>
    expect(setupItems.value.find((item) => item.name === 'claude_login')?.status).toBe('ok'),
  );
  expect(mockBridge.calls).toContainEqual({ method: 'setup/runChecks', params: {} });
});

it("I've checked calls setup/markManual for the manual item", async () => {
  renderChecklist(items([{ name: 'training_off', status: 'manual_pending' }]));
  fireEvent.click(screen.getByRole('button', { name: "I've checked" }));
  await waitFor(() =>
    expect(mockBridge.calls).toContainEqual({
      method: 'setup/markManual',
      params: { item: 'training_off' },
    }),
  );
});

it('show me where expands the instructions', () => {
  renderChecklist(items([{ name: 'training_off', status: 'manual_pending' }]));
  const row = screen
    .getByText('Model training is off in both accounts')
    .closest('.check-row') as HTMLElement;
  fireEvent.click(within(row).getByRole('button', { name: 'Show me where' }));
  expect(within(row).getByText(/Privacy/)).toBeInTheDocument();
});

it('continue is disabled until every item is ok or manual_done', () => {
  renderChecklist(items([{ name: 'training_off', status: 'manual_pending' }]));
  expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();
});

it('a failed item shows its detail and a check again button', () => {
  renderChecklist(items([{ name: 'connector', status: 'failed', detail: 'Missing connector' }]));
  expect(screen.getByText('Missing connector')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Check again' })).toBeInTheDocument();
});

it('signed-in ChatGPT row shows plan and weekly usage from auth/status', async () => {
  setupItems.value = items();
  mockBridge.setResponse('auth/status', async () => auth);
  render(<SetupChecklist />);
  await waitFor(() => expect(screen.getByText(/Plan: Plus/)).toBeInTheDocument());
  expect(screen.getByText(/weekly allowance 12% used/)).toBeInTheDocument();
});
