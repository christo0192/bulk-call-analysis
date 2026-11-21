import { expect, it } from 'vitest';
import { formatKb, stepStatuses } from './Rail';

it.each([
  [0, '1 KB'],
  [1, '1 KB'],
  [1025, '2 KB'],
])('formats %s bytes as %s', (size, result) => expect(formatKb(size)).toBe(result));
it.each([
  ['planned', 'current'],
  ['sample_ready', 'waiting'],
  ['running', 'current'],
  ['paused', 'current'],
  ['done', 'done'],
] as const)('maps %s phase to rail statuses', (state, status) =>
  expect(stepStatuses(state).includes(status)).toBe(true),
);
