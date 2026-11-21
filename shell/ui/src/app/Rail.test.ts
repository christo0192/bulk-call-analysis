import { expect, it } from 'vitest';
import { formatKb, stepStatuses } from './Rail';

it.each([
  [0, '1 KB'],
  [1, '1 KB'],
  [1025, '2 KB'],
])('formats %s bytes as %s', (size, result) => expect(formatKb(size)).toBe(result));
it.each([
  ['draft', 'current'],
  ['planned', 'waiting'],
  ['plan_proposed', 'current'],
  ['plan_approved', 'current'],
  ['running', 'current'],
  ['pulling', 'current'],
  ['masking_sample', 'current'],
  ['sample_running', 'current'],
  ['sample_ready', 'waiting'],
  ['sample_approved', 'current'],
  ['paused', 'current'],
  ['stalled', 'current'],
  ['done', 'done'],
  ['done_with_issues', 'done'],
  ['failed', 'current'],
  ['cancelled', 'current'],
] as const)('rail steps for every engine state: %s', (state, status) =>
  expect(stepStatuses(state).includes(status)).toBe(true),
);
