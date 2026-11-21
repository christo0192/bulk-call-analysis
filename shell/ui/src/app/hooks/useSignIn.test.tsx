import { act, fireEvent, render, screen } from '@testing-library/preact';
import { beforeEach, expect, it, vi } from 'vitest';
import { useSignIn } from './useSignIn';
import { mockBridge } from '../../engine/bridge';
import { setupItems } from '../../state/store';

it('useSignIn polls every 5 seconds, stops when ok, and stops after 5 minutes', async () => {
  vi.useFakeTimers();
  let count = 0;
  mockBridge.calls = [];
  mockBridge.setResponse('auth/status', async () => ({
    claude: { loggedIn: false },
    codex: { loggedIn: false },
  }));
  mockBridge.setResponse('setup/runChecks', async () => ({
    items: [{ name: 'claude_login', status: ++count >= 2 ? 'ok' : 'pending' }],
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
    await vi.advanceTimersByTimeAsync(10000);
  });
  expect(count).toBe(2);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
  });
  expect(count).toBe(2);
  vi.useRealTimers();
});
