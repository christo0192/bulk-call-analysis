import { expect, it } from 'vitest';
import { EngineError } from './client';
it('parses object engine errors including data reason', () => {
  const error = EngineError.from({
    code: -32002,
    message: 'Setup required',
    data: { reason: 'login' },
  });
  expect(error.code).toBe(-32002);
  expect(error.message).toBe('Setup required');
  expect(error.reason).toBe('login');
});
