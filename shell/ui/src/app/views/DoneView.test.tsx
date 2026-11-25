import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/preact';
import outputs from '../../mock/fixtures/engine/job-outputs.json';
import { mockBridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import { DoneView } from './DoneView';
import { MainArea } from '../MainArea';
import { jobsByProject, projects, selectedProject } from '../../state/store';
import { openProjectFile } from '../../engine/shellCommands';

vi.mock('../../engine/shellCommands', () => ({
  openProjectFile: vi.fn(),
  openProjectFolder: vi.fn(),
}));

beforeEach(() => mockBridge.resetForTest());

const renderDone = (rebuildOpen = false) =>
  render(
    <DoneView
      id="job-one"
      project="project-one"
      rebuildOpen={rebuildOpen}
      onCloseRebuild={vi.fn()}
    />,
  );

it('renders the recorded outputs', async () => {
  renderDone();
  // "8" also appears in the reconciliation panel; check the hero container's text.
  const heroText = await screen.findByText(/of 8 leads read/);
  expect(heroText.closest('.done-hero')).toHaveTextContent(/8\s*of 8 leads read/);
  expect(screen.getByRole('button', { name: 'Open results' })).toBeInTheDocument();
});

it('duration includes time asleep', async () => {
  mockBridge.setResponse(
    'job/outputs',
    async () =>
      ({
        ...outputs,
        durationSeconds: 3600,
        asleepSeconds: 1680,
      }) as never,
  );
  renderDone();
  expect(await screen.findByText(/including 28 min asleep/)).toBeInTheDocument();
});

it('newest workbook compares integers', async () => {
  mockBridge.setResponse(
    'job/outputs',
    async () =>
      ({
        ...outputs,
        files: [
          ...outputs.files,
          {
            ...outputs.files[0],
            name: 'results.v2.xlsx',
            path: 'jobs/job-one/output/results.v2.xlsx',
            specVersion: 2,
          },
          {
            ...outputs.files[0],
            name: 'results.v1.rebuild2.xlsx',
            path: 'jobs/job-one/output/results.v1.rebuild2.xlsx',
            rebuild: 2,
          },
        ],
      }) as never,
  );
  renderDone();
  await screen.findByRole('button', { name: 'Open results' });
  fireEvent.click(screen.getByRole('button', { name: 'Open results' }));
  await waitFor(() =>
    expect(openProjectFile).toHaveBeenCalledWith(
      'project-one',
      'jobs/job-one/output/results.v1.xlsx',
    ),
  );
});

it('opens files by their project path', async () => {
  mockBridge.setResponse(
    'job/outputs',
    async () =>
      ({
        ...outputs,
        files: [
          { ...outputs.files[0], path: 'jobs/job-one/output/latest.xlsx' },
          { ...outputs.files[2], path: 'jobs/job-one/output/verify.md' },
          {
            name: 'summary.md',
            path: 'jobs/job-one/output/summary.md',
            kind: 'summary',
            specVersion: 1,
            rebuild: null,
          },
        ],
      }) as never,
  );
  renderDone();
  fireEvent.click(await screen.findByRole('button', { name: 'Open results' }));
  fireEvent.click(screen.getByRole('button', { name: 'Open summary' }));
  fireEvent.click(screen.getByRole('button', { name: 'Open verify report' }));
  await waitFor(() => {
    expect(openProjectFile).toHaveBeenCalledWith('project-one', 'jobs/job-one/output/latest.xlsx');
    expect(openProjectFile).toHaveBeenCalledWith('project-one', 'jobs/job-one/output/summary.md');
    expect(openProjectFile).toHaveBeenCalledWith('project-one', 'jobs/job-one/output/verify.md');
  });
});

it('null values are never shown as zero or 1970', async () => {
  mockBridge.setResponse('job/outputs', async () => ({
    ...outputs,
    finishedAt: null,
    durationSeconds: null,
    reconciliation: null,
  }));
  renderDone();
  const hero = await screen.findByText("The reconciliation isn't available");
  expect(hero.closest('.done-hero')).not.toHaveTextContent('0');
  expect(hero.closest('.done-hero')).not.toHaveTextContent('1970');
});

it('missing reconciliation shows no tick', async () => {
  mockBridge.setResponse('job/outputs', async () => ({ ...outputs, reconciliation: null }));
  renderDone();
  const hero = await screen.findByText("The reconciliation isn't available");
  expect(hero.closest('.done-hero')).not.toHaveTextContent('✓');
});

it('stale answer from another job is ignored', async () => {
  let releaseFirst!: (value: unknown) => void;
  const first = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  mockBridge.setResponse(
    'job/outputs',
    async (params) =>
      ((params as { job: string }).job === 'job-one'
        ? first
        : { ...outputs, headline: 'job two' }) as never,
  );
  const view = renderDone();
  view.rerender(
    <DoneView id="job-two" project="project-one" rebuildOpen={false} onCloseRebuild={vi.fn()} />,
  );
  expect(await screen.findByText('job two')).toBeInTheDocument();
  releaseFirst({ ...outputs, headline: 'stale job one' });
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(screen.queryByText('stale job one')).toBeNull();
});

it('plan failure still shows results', async () => {
  mockBridge.setResponse('job/plan', async () => {
    throw new Error('plan unavailable');
  });
  renderDone();
  expect(await screen.findByRole('button', { name: 'Open results' })).toBeInTheDocument();
});

it('open errors show plain words', async () => {
  vi.mocked(openProjectFile).mockRejectedValueOnce(
    new EngineError(-32000, 'missing', 'file_not_found'),
  );
  renderDone();
  fireEvent.click(await screen.findByRole('button', { name: 'Open results' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('That file is no longer there');

  vi.mocked(openProjectFile).mockRejectedValueOnce(
    new EngineError(-32000, 'blocked', 'type_not_allowed'),
  );
  fireEvent.click(screen.getByRole('button', { name: 'Open results' }));
  expect(await screen.findByRole('alert')).toHaveTextContent("The app can't open that file type");
});

it('reloads after a rebuild', async () => {
  let releaseOutputs!: () => void;
  const outputsReloaded = new Promise<void>((resolve) => {
    releaseOutputs = resolve;
  });
  let outputsCalls = 0;
  mockBridge.setResponse('job/outputs', async () => {
    outputsCalls += 1;
    if (outputsCalls === 2) await outputsReloaded;
    return outputsCalls === 2 ? { ...outputs, headline: 'Reloaded results' } : outputs;
  });
  renderDone(true);
  await screen.findByRole('button', { name: 'Rebuild' });
  fireEvent.click(screen.getByRole('button', { name: 'Rebuild' }));
  // The dev plan carries rules, so the rebuild goes straight through (no empty-rules prompt).
  expect(await screen.findByText(/Rebuilt: results\.v2\.rebuild1\.xlsx/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Open' })).toBeInTheDocument();
  releaseOutputs();
  expect(await screen.findByText('Reloaded results')).toBeInTheDocument();
  expect(screen.getByText(/Rebuilt: results\.v2\.rebuild1\.xlsx/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Open' })).toBeInTheDocument();
});

it('reconciliation lists non-zero terms and adds up', async () => {
  mockBridge.setResponse('job/outputs', async () => ({
    ...outputs,
    reconciliation: {
      ...outputs.reconciliation!,
      selected: 10,
      read: 8,
      noTranscript: 1,
      heldBack: 1,
    },
  }));
  renderDone();
  const panel = await screen.findByText('Every lead accounted for');
  const reconciliation = panel.closest('.reconciliation') as HTMLElement;
  expect(within(reconciliation).getByText('Leads selected')).toBeInTheDocument();
  expect(within(reconciliation).getByText('No transcript')).toBeInTheDocument();
  expect(within(reconciliation).getByText('Held back')).toBeInTheDocument();
  expect(within(reconciliation).queryByText('Transcription failed')).not.toBeInTheDocument();
  expect(within(reconciliation).getByText(/Adds up: 10/)).toBeInTheDocument();
});

it('does not add up shows the warning', async () => {
  mockBridge.setResponse('job/outputs', async () => ({
    ...outputs,
    reconciliation: { ...outputs.reconciliation!, addsUp: false },
  }));
  renderDone();
  expect(await screen.findByText(/Doesn't add up/)).toBeInTheDocument();
});

it('issues view lists lead ids', async () => {
  mockBridge.setResponse('job/outputs', async () => ({
    ...outputs,
    issues: [{ kind: 'flagged', count: 1, text: 'Flagged lead', leads: ['lead-9'] }],
  }));
  renderDone();
  fireEvent.click(await screen.findByRole('button', { name: 'View' }));
  expect(screen.getByText('lead-9')).toBeInTheDocument();
});

it('issue list says first 50', async () => {
  mockBridge.setResponse('job/outputs', async () => ({
    ...outputs,
    issues: [
      {
        kind: 'flagged',
        count: 75,
        text: 'Flagged leads',
        leads: Array.from({ length: 75 }, (_, i) => `lead-${i + 1}`),
      },
    ],
  }));
  renderDone();
  fireEvent.click(await screen.findByRole('button', { name: 'View' }));
  expect(screen.getByText('First 50 of 75')).toBeInTheDocument();
  expect(screen.getByText(/lead-50/)).toBeInTheDocument();
  expect(screen.queryByText(/lead-51/)).toBeNull();
});

it('run again focuses the composer with a prefix', async () => {
  projects.value = [{ project: 'project-one', title: 'Project one', status: 'done' }];
  jobsByProject.value = {
    'project-one': [{ job: 'job-one', project: 'project-one', state: 'done' }],
  };
  selectedProject.value = 'project-one';
  mockBridge.setResponse('chat/history', async () => ({ entries: [], more: false }));
  render(<MainArea />);
  fireEvent.click(await screen.findByRole('button', { name: 'Run again with new dates' }));
  const composer = screen.getByPlaceholderText(/Ask Claude/);
  expect(composer).toHaveValue('Run again for new dates: ');
  expect(document.activeElement).toBe(composer);
});
