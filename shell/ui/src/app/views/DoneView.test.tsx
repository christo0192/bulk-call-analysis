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
  mockBridge.setResponse('job/outputs', async () => ({
    ...outputs,
    durationSeconds: 3600,
    asleepSeconds: 1680,
  }));
  renderDone();
  expect(await screen.findByText(/including 28 min asleep/)).toBeInTheDocument();
});

it('open results calls open_project_file with the newest workbook', async () => {
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

it('rebuild success stays visible while outputs reload', async () => {
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
  // The recorded plan has no rules, so the dialog asks before rebuilding with none.
  fireEvent.click(await screen.findByRole('button', { name: 'Rebuild without rules' }));
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
