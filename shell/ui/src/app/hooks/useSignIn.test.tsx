import { act, fireEvent, render, screen } from '@testing-library/preact';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { useSignIn, signInCheckMessage } from './useSignIn';
import { EngineError } from '../../engine/client';
import { mockBridge } from '../../engine/bridge';
import { setupItems } from '../../state/store';

beforeEach(() => {
  mockBridge.calls = [];
  mockBridge.clearResponses();
});

afterEach(() => {
  vi.useRealTimers();
});

it('useSignIn polls runChecks every 5 seconds and auth status at most every 15 seconds', async () => {
  vi.useFakeTimers();
  mockBridge.setResponse('auth/status', async () => ({
    claude: { loggedIn: false },
    codex: { loggedIn: false },
  }));
  mockBridge.setResponse('setup/runChecks', async () => ({
    items: [{ name: 'claude_login', status: 'pending' }],
  }));
  function Probe() {
    const sign = useSignIn(
      () => undefined,
      () => undefined,
    );
    return <button onClick={() => void sign.signIn('claude')}>start</button>;
  }
  render(<Probe />);
  fireEvent.click(screen.getByRole('button', { name: 'start' }));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(mockBridge.calls.filter((call) => call.method === 'setup/runChecks')).toHaveLength(1);
  expect(mockBridge.calls.filter((call) => call.method === 'auth/status')).toHaveLength(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10000);
  });
  expect(mockBridge.calls.filter((call) => call.method === 'setup/runChecks')).toHaveLength(3);
  expect(mockBridge.calls.filter((call) => call.method === 'auth/status')).toHaveLength(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(mockBridge.calls.filter((call) => call.method === 'setup/runChecks')).toHaveLength(4);
  expect(mockBridge.calls.filter((call) => call.method === 'auth/status')).toHaveLength(2);
});

it('check again runs immediately even right after a poll', async () => {
  vi.useFakeTimers();
  mockBridge.setResponse('auth/status', async () => ({
    claude: { loggedIn: false },
    codex: { loggedIn: false },
  }));
  mockBridge.setResponse('setup/runChecks', async () => ({
    items: [{ name: 'claude_login', status: 'pending' }],
  }));
  function Probe() {
    const sign = useSignIn(
      () => undefined,
      () => undefined,
    );
    return (
      <>
        <button onClick={() => void sign.signIn('claude')}>start</button>
        {sign.waitingFor && <button onClick={() => sign.checkAgain('claude')}>Check again</button>}
      </>
    );
  }
  render(<Probe />);
  fireEvent.click(screen.getByRole('button', { name: 'start' }));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(mockBridge.calls.filter((call) => call.method === 'setup/runChecks')).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
  await act(async () => Promise.resolve());
  expect(mockBridge.calls.filter((call) => call.method === 'setup/runChecks')).toHaveLength(2);
  expect(mockBridge.calls.filter((call) => call.method === 'auth/status')).toHaveLength(2);
});

it('check again is disabled while a check is in flight', async () => {
  vi.useFakeTimers();
  let resolveCheck: (() => void) | undefined;
  mockBridge.setResponse('auth/status', async () => ({
    claude: { loggedIn: false },
    codex: { loggedIn: false },
  }));
  mockBridge.setResponse(
    'setup/runChecks',
    () =>
      new Promise((resolve) => {
        resolveCheck = () => resolve({ items: [{ name: 'claude_login', status: 'pending' }] });
      }),
  );
  function Probe() {
    const sign = useSignIn(
      () => undefined,
      () => undefined,
    );
    return (
      <>
        <button onClick={() => void sign.signIn('claude')}>start</button>
        {sign.waitingFor && (
          <button disabled={sign.isChecking} onClick={() => sign.checkAgain('claude')}>
            {sign.isChecking ? 'Checking…' : 'Check again'}
          </button>
        )}
      </>
    );
  }
  render(<Probe />);
  fireEvent.click(screen.getByRole('button', { name: 'start' }));
  fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
  expect(screen.getByRole('button', { name: 'Checking…' })).toBeDisabled();
  expect(mockBridge.calls.filter((call) => call.method === 'setup/runChecks')).toHaveLength(1);
  resolveCheck?.();
  await act(async () => Promise.resolve());
});

it('sign-in polling never overlaps a slow check', async () => {
  vi.useFakeTimers();
  let resolveCheck: (() => void) | undefined;
  mockBridge.setResponse('auth/status', async () => ({
    claude: { loggedIn: false },
    codex: { loggedIn: false },
  }));
  mockBridge.setResponse(
    'setup/runChecks',
    () =>
      new Promise((resolve) => {
        resolveCheck = () => resolve({ items: [{ name: 'claude_login', status: 'pending' }] });
      }),
  );
  function Probe() {
    const sign = useSignIn(
      () => undefined,
      () => undefined,
    );
    return <button onClick={() => void sign.signIn('claude')}>start</button>;
  }
  render(<Probe />);
  fireEvent.click(screen.getByRole('button', { name: 'start' }));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60000);
  });
  expect(mockBridge.calls.filter((call) => call.method === 'setup/runChecks')).toHaveLength(1);
  resolveCheck?.();
  await act(async () => Promise.resolve());
  vi.useRealTimers();
});

it('a failed sign-in check is explained in plain words', () => {
  // As the engine raises it, and as the Tauri bridge passes it on as text.
  const structured = new EngineError(
    -32001,
    'Codex app-server did not start',
    'app_server_unavailable',
  );
  const text = 'RPC error -32001: Codex app-server did not start';
  for (const cause of [structured, text]) {
    const message = signInCheckMessage(cause);
    expect(message).toContain("Couldn't check the ChatGPT sign-in");
    expect(message).not.toContain('app-server');
  }
  // An engine without auth/status yet: say nothing (the rows already show "Not signed in yet").
  expect(signInCheckMessage(new EngineError(-32601, 'Method not found'))).toBe('');
  expect(signInCheckMessage(new EngineError(-32000, 'boom'))).toBe(
    "Couldn't check the sign-in status (boom). Press Check again.",
  );
});
