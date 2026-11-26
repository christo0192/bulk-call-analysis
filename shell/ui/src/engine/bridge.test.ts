import { afterEach, expect, it, vi } from 'vitest';
import { clearMocks, mockIPC } from '@tauri-apps/api/mocks';
import { emit } from '@tauri-apps/api/event';
import type { Notification } from './types';

afterEach(() => {
  clearMocks();
  // Back to a plain browser window for the next test.
  Reflect.deleteProperty(window, '__TAURI_INTERNALS__');
  Reflect.deleteProperty(window, '__TAURI_EVENT_PLUGIN_INTERNALS__');
  vi.unstubAllEnvs();
  vi.doUnmock('./mockBridge');
  vi.resetModules();
});

/** The bridge module evaluated afresh, as the app would load it in this window and build. */
async function freshBridge() {
  vi.resetModules();
  return import('./bridge');
}

/** A desktop window: every Tauri command is recorded, and events can be emitted. */
function tauriWindow(): Array<[string, unknown]> {
  const commands: Array<[string, unknown]> = [];
  mockIPC(
    (command, payload) => {
      commands.push([command, payload]);
      return command === 'engine_call' ? { status: 'ok' } : null;
    },
    { shouldMockEvents: true },
  );
  return commands;
}

it('outside Tauri, in development and tests, the bridge talks to the mock', async () => {
  const { bridge, mockBridgeLoaded } = await freshBridge();
  const { mockBridge } = await import('./mockBridge');
  expect(await mockBridgeLoaded).toBe(mockBridge);
  await bridge.call('health/ping', {});
  expect(mockBridge.calls.at(-1)?.method).toBe('health/ping');
  const { openProjectFolder } = await import('./shellCommands');
  await openProjectFolder('project-one');
  expect(mockBridge.shellCalls).toContainEqual({
    command: 'open_project_folder',
    params: { project: 'project-one' },
  });
});

it('listeners added while the mock is still loading hear it once it has loaded', async () => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => (release = resolve));
  vi.resetModules();
  vi.doMock('./mockBridge', async () => {
    await gate;
    return vi.importActual<typeof import('./mockBridge')>('./mockBridge');
  });
  const { bridge, mockBridgeLoaded } = await import('./bridge');
  const heard: string[] = [];
  bridge.onNotification((notification) => heard.push(notification.method));
  const removeEarly = bridge.onNotification((notification) =>
    heard.push(`removed early: ${notification.method}`),
  );
  removeEarly();
  const status = bridge.status();
  release();
  const mock = await mockBridgeLoaded!;
  expect(await status).toBe('ready');
  mock.emit({ jsonrpc: '2.0', method: 'job/state', params: {} });
  expect(heard).toEqual(['job/state']);
});

it('inside Tauri the bridge is the Tauri bridge itself and the mock is never loaded', async () => {
  const commands = tauriWindow();
  vi.stubEnv('VITE_BRIDGE', '');
  const { bridge, mockBridgeLoaded } = await freshBridge();
  const { tauriBridge } = await import('./tauriBridge');
  expect(mockBridgeLoaded).toBeNull();
  expect(bridge).toBe(tauriBridge);
  expect(await bridge.call('health/ping', {})).toEqual({ status: 'ok' });
  expect(commands).toContainEqual(['engine_call', { method: 'health/ping', params: {} }]);
  // Engine notifications arrive as the shell's engine-event.
  const heard: Notification[] = [];
  bridge.onNotification((notification) => heard.push(notification));
  const note: Notification = {
    jsonrpc: '2.0',
    method: 'job/state',
    params: { job: 'synthetic-job-1' },
  };
  await vi.waitFor(async () => {
    if (!heard.length) await emit('engine-event', note);
    expect(heard).toEqual([note]);
  });
  // Shell commands go to Tauri too.
  const { openProjectFile } = await import('./shellCommands');
  await openProjectFile('project-one', 'results.xlsx');
  expect(commands).toContainEqual([
    'open_project_file',
    { project: 'project-one', name: 'results.xlsx' },
  ]);
});

it('VITE_BRIDGE=mock uses the mock inside a Tauri dev window', async () => {
  const commands = tauriWindow();
  vi.stubEnv('VITE_BRIDGE', 'mock');
  const { bridge, mockBridgeLoaded } = await freshBridge();
  const mock = await mockBridgeLoaded!;
  await bridge.call('health/ping', {});
  expect(mock.calls.at(-1)?.method).toBe('health/ping');
  expect(commands.filter(([command]) => command === 'engine_call')).toEqual([]);
});

it('a production build has no mock: outside Tauri it says the engine is unavailable', async () => {
  vi.stubEnv('DEV', false);
  vi.stubEnv('PROD', true);
  const { bridge, mockBridgeLoaded } = await freshBridge();
  expect(mockBridgeLoaded).toBeNull();
  expect(await bridge.status()).toBe('error');
  await expect(bridge.call('projects/list', {})).rejects.toMatchObject({
    code: null,
    message: 'The engine runs only inside the desktop app.',
  });
  const { openProjectFolder } = await import('./shellCommands');
  await expect(openProjectFolder('project-one')).rejects.toMatchObject({ code: -32601 });
});

it('the production desktop app uses the Tauri bridge, calls and shell commands included', async () => {
  // The shipped app: a production build inside Tauri. Nothing may wait on the mock there.
  const commands = tauriWindow();
  vi.stubEnv('DEV', false);
  vi.stubEnv('PROD', true);
  vi.stubEnv('VITE_BRIDGE', '');
  const { bridge, mockBridgeLoaded } = await freshBridge();
  const { tauriBridge } = await import('./tauriBridge');
  expect(mockBridgeLoaded).toBeNull();
  expect(bridge).toBe(tauriBridge);
  expect(await bridge.call('health/ping', {})).toEqual({ status: 'ok' });
  expect(commands).toContainEqual(['engine_call', { method: 'health/ping', params: {} }]);
  const { openProjectFile } = await import('./shellCommands');
  await openProjectFile('project-one', 'results.xlsx');
  expect(commands).toContainEqual([
    'open_project_file',
    { project: 'project-one', name: 'results.xlsx' },
  ]);
});
