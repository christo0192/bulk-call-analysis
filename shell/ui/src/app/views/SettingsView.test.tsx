import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { beforeEach, describe, expect, it } from 'vitest';
import { SettingsView } from './SettingsView';
import { mockBridge } from '../../engine/bridge';
import { settingsOpen } from '../../state/store';

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
it('saving the connector path calls settings/set and shows Saved', async () => {
  view();
  await screen.findByDisplayValue('');
  fireEvent.input(screen.getByLabelText('Connector path'), { target: { value: 'connector.exe' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(screen.getByText('Saved')).toBeInTheDocument());
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
it('workers can be Automatic or a number from 1 to 8', async () => {
  view();
  await screen.findByLabelText('Worker count');
  expect(screen.getByRole('option', { name: 'Automatic' })).toBeInTheDocument();
  expect(screen.getAllByRole('option')).toHaveLength(9);
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
  await screen.findByLabelText('Connector path');
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
