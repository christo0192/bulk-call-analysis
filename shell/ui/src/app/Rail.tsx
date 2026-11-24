import { useEffect, useState } from 'preact/hooks';
import { bridge } from '../engine/bridge';
import type { FileInfo } from '../engine/types';
import { FileIcon } from './ui/icons';
export type StepStatus = 'done' | 'current' | 'waiting' | 'todo';
export const stepNames = [
  'Plan',
  'Pull and mask transcripts',
  'Read a 20-lead sample',
  'You check the sample',
  'Full run',
  'Quote check and repair',
  'Excel built and verified',
  'Summary written',
];
export function stepStatuses(state: string | undefined): StepStatus[] {
  const index =
    state === 'plan_approved'
      ? 1
      : state === 'sample_ready'
        ? 3
        : state === 'sample_approved'
          ? 4
          : [
                'running',
                'pulling',
                'masking_sample',
                'sample_running',
                'paused',
                'stalled',
              ].includes(state ?? '')
            ? 4
            : state === 'done' || state === 'done_with_issues'
              ? 8
              : 0;
  const statuses = stepNames.map(
    (_, i) => (i < index ? 'done' : i === index ? 'current' : 'todo') as StepStatus,
  );
  if (state === 'planned') statuses[0] = 'waiting';
  if (state === 'sample_ready') statuses[index] = 'waiting';
  if (
    state === 'plan_approved' ||
    state === 'running' ||
    state === 'pulling' ||
    state === 'masking_sample'
  ) {
    statuses[1] = 'current';
    statuses[2] = 'current';
  }
  return statuses;
}
export function formatKb(size: number): string {
  return `${Math.max(1, Math.ceil(size / 1024))} KB`;
}
export function Rail({ project, state }: { project: string; state?: string }) {
  const [filesByProject, setFilesByProject] = useState<Record<string, FileInfo[]>>({});
  const [fileErrors, setFileErrors] = useState<Record<string, string>>({});
  const loadFiles = (active: () => boolean) =>
    void bridge
      .call('files/list', { project })
      .then((value) => {
        if (active()) {
          setFilesByProject((current) => ({ ...current, [project]: value }));
          setFileErrors((current) => ({ ...current, [project]: '' }));
        }
      })
      .catch((reason) => {
        const message = reason instanceof Error ? reason.message : String(reason);
        console.warn(`Couldn't load files for ${project}: ${message}`);
        if (active())
          setFileErrors((current) => ({ ...current, [project]: "Couldn't load files" }));
      });
  useEffect(() => {
    let active = true;
    loadFiles(() => active);
    const unsubscribe = bridge.onNotification((notification) => {
      if (notification.method !== 'job/state') return;
      const params = notification.params as { project?: string };
      if (params.project === project) loadFiles(() => active);
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, [project]);
  const statuses = stepStatuses(state);
  return (
    <aside class="rail">
      <h3>This job</h3>
      <ol class="steps">
        {stepNames.map((name, i) => (
          <li class={statuses[i]} key={name}>
            <span class="step-marker">{statuses[i] === 'done' ? '✓' : ''}</span>
            <span>
              {name}
              {statuses[i] === 'waiting' && <small>waiting for you</small>}
            </span>
          </li>
        ))}
      </ol>
      <h3>Files</h3>
      {fileErrors[project] && <small class="muted">{fileErrors[project]}</small>}
      <ul class="files">
        {(filesByProject[project] ?? []).map((file) => (
          <li key={file.name}>
            <FileIcon />
            <span>{file.name}</span>
            <span class="size">{formatKb(file.size)}</span>
          </li>
        ))}
      </ul>
    </aside>
  );
}
