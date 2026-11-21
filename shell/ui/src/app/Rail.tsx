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
  if (state === 'sample_ready') statuses[index] = 'waiting';
  if (state === 'running' || state === 'pulling' || state === 'masking_sample') {
    statuses[1] = 'current';
    statuses[2] = 'current';
  }
  return statuses;
}
export function formatKb(size: number): string {
  return `${Math.max(1, Math.ceil(size / 1024))} KB`;
}
export function Rail({ project, state }: { project: string; state?: string }) {
  const [files, setFiles] = useState<FileInfo[]>([]);
  useEffect(() => {
    let active = true;
    void bridge.call('files/list', { project }).then((value) => {
      if (active) setFiles(value);
    });
    return () => {
      active = false;
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
      <ul class="files">
        {files.map((file) => (
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
