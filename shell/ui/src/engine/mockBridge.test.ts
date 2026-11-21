import { describe, it, expect } from 'vitest';
import { MockBridge } from './mockBridge';
describe('mock bridge', () => {
  it('records calls', async () => {
    const b = new MockBridge();
    await b.call('health/ping', {});
    expect(b.calls[0]?.method).toBe('health/ping');
  });
  it('uses response override', async () => {
    const b = new MockBridge();
    b.setResponse('health/ping', () => ({ status: 'ok' }));
    expect(await b.call('health/ping', {})).toEqual({ status: 'ok' });
  });
  it('emits notification', () => {
    const b = new MockBridge();
    let seen = '';
    b.onNotification((n) => {
      seen = n.method;
    });
    b.emit({ jsonrpc: '2.0', method: 'job/state', params: {} });
    expect(seen).toBe('job/state');
  });
  it('unsubscribes', () => {
    const b = new MockBridge();
    let n = 0;
    const off = b.onNotification(() => n++);
    off();
    b.emit({ jsonrpc: '2.0', method: 'job/state', params: {} });
    expect(n).toBe(0);
  });
  it('lists synthetic projects', async () => {
    const b = new MockBridge();
    expect((await b.call('projects/list', {})).length).toBeGreaterThan(0);
  });
});
it('supports async response overrides', async () => {
  const b = new MockBridge();
  b.setResponse('health/ping', async () => ({ status: 'ok' }));
  await expect(b.call('health/ping', {})).resolves.toEqual({ status: 'ok' });
});
it('records parameters', async () => {
  const b = new MockBridge();
  await b.call('projects/jobs', { project: 'demo-run' });
  expect(b.calls.at(-1)?.params).toEqual({ project: 'demo-run' });
});
