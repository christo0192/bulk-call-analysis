import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { beforeEach, describe, expect, it } from 'vitest';
import { SettingsView } from './SettingsView';
import { mockBridge } from '../../engine/bridge';
import { settingsOpen, setupRequired } from '../../state/store';
import { App } from '../App';

const settings = {
  connectorPath: '',
  keyLocated: true,
  claudeModel: 'claude-sonnet',
  codexModel: 'gpt-5',
  workerCount: 0,
  retentionDays: 30,
};
const auth = {
  claude: { loggedIn: true },
  codex: { loggedIn: true, planType: 'Plus', usagePercent: 12 },
};
const app = {
  state: 'ready',
  protocol: 'json-rpc',
  engineVersion: '0.1.0',
  dataDir: 'Application data',
  runningJobs: 0,
};
function view() {
  settingsOpen.value = true;
  return render(<SettingsView />);
}
beforeEach(() => {
  mockBridge.calls = [];
  mockBridge.setResponse('settings/get', async () => ({ ...settings }));
  mockBridge.setResponse('settings/set', async (changes) => ({ ...settings, ...changes }));
  mockBridge.setResponse('auth/status', async () => auth);
  mockBridge.setResponse('app/status', async () => app);
});

it('renders every section from the fixtures', async () => {
  view();
  for (const label of ['Sign-in', 'BigQuery', 'Models', 'Workers', 'Retention', 'Setup', 'About'])
    expect(await screen.findByText(label)).toBeInTheDocument();
});
it('saving the key file path calls settings/set and shows Saved', async () => {
  view();
  await screen.findByDisplayValue('');
  fireEvent.input(screen.getByLabelText('Service-account key file'), {
    target: { value: 'connector.exe' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(screen.getByText('Saved')).toBeInTheDocument());
  // Screen readers hear it too.
  expect(screen.getByRole('status')).toHaveTextContent('Saved');
  expect(mockBridge.calls).toContainEqual({
    method: 'settings/set',
    params: { connectorPath: 'connector.exe' },
  });
});
it('a model change needs confirmation and sends confirmModelChange true', async () => {
  view();
  await screen.findByText('claude-sonnet');
  fireEvent.click(screen.getAllByRole('button', { name: /Change/ })[0]!);
  fireEvent.input(screen.getByLabelText('New model'), { target: { value: 'new-model' } });
  fireEvent.click(screen.getByRole('button', { name: 'Change model' }));
  await waitFor(() =>
    expect(mockBridge.calls).toContainEqual({
      method: 'settings/set',
      params: { claudeModel: 'new-model', confirmModelChange: true },
    }),
  );
});
it('cancelling a model change sends nothing', async () => {
  view();
  await screen.findByText('claude-sonnet');
  fireEvent.click(screen.getAllByRole('button', { name: /Change/ })[0]!);
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(mockBridge.calls.some((call) => call.method === 'settings/set')).toBe(false);
});
it('workers offers 1 to 8 and saves the choice', async () => {
  view();
  await screen.findByLabelText('Worker count');
  expect(screen.getAllByRole('option')).toHaveLength(8);
  fireEvent.change(screen.getByLabelText('Worker count'), { target: { value: '3' } });
  await waitFor(() =>
    expect(mockBridge.calls).toContainEqual({ method: 'settings/set', params: { workerCount: 3 } }),
  );
  expect(screen.getByText('Saved')).toBeInTheDocument();
});
it('retention outside 1-365 is rejected before sending', async () => {
  view();
  await screen.findByLabelText(/Keep masked/);
  fireEvent.change(screen.getByLabelText(/Keep masked/), { target: { value: '366' } });
  expect(mockBridge.calls.some((call) => call.method === 'settings/set')).toBe(false);
});
it('an invalid value error shows "That value isn\'t valid"', async () => {
  mockBridge.setResponse('settings/set', async () => {
    throw { code: -32602, message: 'bad' };
  });
  view();
  await screen.findByLabelText('Service-account key file');
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  expect(await screen.findByText("That value isn't valid")).toBeInTheDocument();
});
it('load failure shows an error card and Try again reloads', async () => {
  let fail = true;
  mockBridge.setResponse('settings/get', async () => {
    if (fail) throw new Error('offline');
    return { ...settings };
  });
  view();
  expect(await screen.findByText("Couldn't load settings: offline")).toBeInTheDocument();
  fail = false;
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  await waitFor(() => expect(screen.getByText('BigQuery')).toBeInTheDocument());
});
it('back returns to the main area', async () => {
  view();
  await screen.findByText('Settings');
  fireEvent.click(screen.getByRole('button', { name: 'Back' }));
  expect(settingsOpen.value).toBe(false);
});
it('key located shows Key found and never the key path', async () => {
  view();
  expect(await screen.findByText('Key found')).toBeInTheDocument();
  expect(screen.queryByText(/keyLocated|keyFound|\.json/)).not.toBeInTheDocument();
});

it('check setup again opens the checklist and runs the checks', async () => {
  settingsOpen.value = true;
  mockBridge.setResponse('projects/list', async () => []);
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'Check setup again' }));
  expect(setupRequired.value).toBe(true);
  expect(await screen.findByText('Set up this computer')).toBeInTheDocument();
  await waitFor(() =>
    expect(mockBridge.calls.some((call) => call.method === 'setup/runChecks')).toBe(true),
  );
});

it('key file field is labelled and shows found or not found', async () => {
  mockBridge.setResponse('settings/get', async () => ({ ...settings, keyLocated: false }));
  view();
  expect(await screen.findByLabelText('Service-account key file')).toBeInTheDocument();
  expect(await screen.findByText('Key not found')).toBeInTheDocument();
});

it('unknown plan and usage show unknown', async () => {
  mockBridge.setResponse('auth/status', async () => ({
    claude: { loggedIn: true },
    codex: { loggedIn: true },
  }));
  // Usage now also comes from allowance/status, so it must be unknown there too.
  mockBridge.setResponse('allowance/status', async () => ({ codex: null, claude: null }));
  view();
  expect(await screen.findByText(/Plan: unknown/)).toBeInTheDocument();
  expect(screen.getByText(/weekly allowance unknown/)).toBeInTheDocument();
  expect(screen.queryByText(/unknown used/)).toBeNull();
});
