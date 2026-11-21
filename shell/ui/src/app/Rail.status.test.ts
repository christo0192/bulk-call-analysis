import { expect, it } from 'vitest';
import { stepNames, stepStatuses } from './Rail';

it('has the complete job step list', () => expect(stepNames).toHaveLength(8));
it('waits for approval on a planned job', () => expect(stepStatuses('planned')[0]).toBe('waiting'));
it('marks plan approval as active pull work', () =>
  expect(stepStatuses('plan_approved').slice(1, 3)).toEqual(['current', 'current']));
it('marks running pull work as active', () =>
  expect(stepStatuses('running').slice(1, 3)).toEqual(['current', 'current']));
it('marks the sample check as waiting', () =>
  expect(stepStatuses('sample_ready')[3]).toBe('waiting'));
it('marks full run as ready after sample approval', () =>
  expect(stepStatuses('sample_approved')[4]).toBe('current'));
it('marks paused work as active full run', () => expect(stepStatuses('paused')[4]).toBe('current'));
it('marks stalled work as active full run', () =>
  expect(stepStatuses('stalled')[4]).toBe('current'));
it('marks completed work as done', () =>
  expect(stepStatuses('done').every((status) => status === 'done')).toBe(true));
it('marks completed-with-issues work as done', () =>
  expect(stepStatuses('done_with_issues').every((status) => status === 'done')).toBe(true));
it('leaves an unknown job at the plan step', () =>
  expect(stepStatuses('unknown')[0]).toBe('current'));
