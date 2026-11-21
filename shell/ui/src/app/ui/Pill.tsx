import type { JobState } from '../../engine/types';
export const statusLabels: Record<JobState, string> = {
  planned: 'Setting up',
  plan_approved: 'Preparing',
  sample_ready: 'Sample ready',
  running: 'Reading',
  paused: 'Paused',
  stalled: 'Stalled',
  done: 'Done',
  done_with_issues: 'Done with issues',
  failed: 'Failed',
  cancelled: 'Cancelled',
};
export function pillClass(state?: JobState): string {
  if (state === 'running' || state === 'sample_ready')
    return state === 'running' ? 'active' : 'setup';
  if (state === 'paused' || state === 'done_with_issues') return 'amber';
  if (state === 'stalled' || state === 'failed') return 'danger';
  if (state === 'done') return 'active';
  return 'setup';
}
export function Pill({ state }: { state?: JobState }) {
  const actual = state ?? 'planned';
  return <span class={`pill ${pillClass(actual)}`}>{statusLabels[actual]}</span>;
}
