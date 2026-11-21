import { expect, it } from 'vitest';
import { EngineError } from './client';
it('EngineError.from keeps an existing EngineError unchanged', () => {
  const original = new EngineError(-32002, 'Setup required');
  expect(EngineError.from(original)).toBe(original);
});

it('EngineError.from parses the shell text form RPC error code and message', () => {
  const error = EngineError.from('RPC error -32002: Setup required');
  expect(error.code).toBe(-32002);
  expect(error.message).toBe('Setup required');
});

it('EngineError.from parses the object form with data.reason', () => {
  const error = EngineError.from({
    code: -32002,
    message: 'Setup required',
    data: { reason: 'login' },
  });
  expect(error.code).toBe(-32002);
  expect(error.message).toBe('Setup required');
  expect(error.reason).toBe('login');
});
