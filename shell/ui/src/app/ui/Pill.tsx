export const statusLabels: Record<string, string> = {
  draft: 'Setting up',
  planned: 'Setting up',
  plan_proposed: 'Setting up',
  plan_approved: 'Preparing',
  sample_ready: 'Sample ready',
  sample_approved: 'Ready to start',
  running: 'Reading',
  paused: 'Paused',
  stalled: 'Stalled',
  done: 'Done',
  done_with_issues: 'Done with issues',
  failed: 'Failed',
  cancelled: 'Cancelled',
};
export function pillClass(state?: string): string {
  if (state && !Object.prototype.hasOwnProperty.call(statusLabels, state)) return 'neutral';
  if (state === 'running' || state === 'sample_ready')
    return state === 'running' ? 'active' : 'setup';
  if (state === 'paused' || state === 'done_with_issues') return 'amber';
  if (state === 'stalled' || state === 'failed') return 'danger';
  if (state === 'done') return 'active';
  return 'setup';
}
export function Pill({ state }: { state?: string }) {
  const actual = state ?? 'planned';
  return <span class={`pill ${pillClass(actual)}`}>{statusLabels[actual] ?? actual}</span>;
}
