import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/preact';
import running from '../mock/fixtures/engine/job-snapshot-running.json';
import { jobs as fixtureJobs, projects as fixtureProjects } from '../mock/fixtures/data';
import { mockBridge } from '../engine/mockBridge';
import type { JobSummary } from '../engine/types';
import {
  jobsByProject,
  newProjectOpen,
  projects,
  selectedProject,
  settingsOpen,
  setupItems,
  setupRequired,
} from '../state/store';
import { App } from './App';
import { Sidebar } from './Sidebar';
import { MainArea } from './MainArea';
import { Rail } from './Rail';
import { Pill, statusLabels } from './ui/Pill';
import { Chat } from './chat/Chat';
import { RunView } from './views/RunView';
import { SettingsView } from './views/SettingsView';
import { SetupChecklist } from './views/SetupChecklist';

vi.mock('../engine/shellCommands', () => ({
  openProjectFile: vi.fn(),
  openProjectFolder: vi.fn(),
}));

beforeEach(() => {
  mockBridge.resetForTest();
  localStorage.clear();
  settingsOpen.value = false;
  setupRequired.value = false;
  newProjectOpen.value = false;
  // Copies: the mock changes its own job records as a test approves things.
  projects.value = fixtureProjects.map((project) => ({ ...project }));
  jobsByProject.value = Object.fromEntries(
    fixtureProjects.map((project) => [
      project.project,
      fixtureJobs.filter((job) => job.project === project.project).map((job) => ({ ...job })),
    ]),
  );
});

/** The window as the app lays it out: the sidebar, then the main area. */
function frame(project: string | null) {
  selectedProject.value = project;
  return render(
    <div class="window">
      <Sidebar />
      <MainArea />
    </div>,
  );
}
function setJobState(project: string, state: JobSummary['state']) {
  jobsByProject.value = {
    ...jobsByProject.value,
    [project]: (jobsByProject.value[project] ?? []).map((job) => ({ ...job, state })),
  };
}
/** Elements Tab stops on, in document order. */
function tabStops(): HTMLElement[] {
  return [
    ...document.body.querySelectorAll<HTMLElement>(
      'button, input, select, textarea, a[href], [tabindex]',
    ),
  ].filter((element) => {
    if ((element as HTMLButtonElement).disabled) return false;
    const index = element.getAttribute('tabindex');
    return index === null || Number(index) >= 0;
  });
}
/** The text a screen reader reads: without the parts hidden from it. */
function spokenText(element: Element): string {
  const copy = element.cloneNode(true) as Element;
  copy.querySelectorAll('[aria-hidden="true"]').forEach((hidden) => hidden.remove());
  return copy.textContent ?? '';
}
/** Buttons a screen reader would announce with no words (empty, or only a symbol like ×). */
function unnamedButtons(): string[] {
  const named = new Set(screen.queryAllByRole('button', { name: /[\p{L}\p{N}]/u }));
  return screen
    .queryAllByRole('button')
    .filter((button) => !named.has(button))
    .map((button) => button.outerHTML);
}

it('tab order goes sidebar, main, drawer', async () => {
  const order = () => {
    const regions = tabStops().map((element) =>
      element.closest('.sidebar')
        ? 'sidebar'
        : element.closest('.drawer, .chat-tab')
          ? 'drawer'
          : element.closest('main')
            ? 'main'
            : 'elsewhere',
    );
    // Each part as one unbroken run of Tab stops.
    return regions.filter((region, index) => region !== regions[index - 1]);
  };
  // The run view, with the chat drawer open.
  const run = frame('demo-run');
  await screen.findByText(/of 8 leads read/);
  expect(screen.getByRole('button', { name: 'Hide chat' })).toBeInTheDocument();
  expect(order()).toEqual(['sidebar', 'main', 'drawer']);
  run.unmount();
  // The done view has buttons of its own, and the chat is the tab at the edge.
  frame('demo-done');
  await screen.findByRole('button', { name: 'Open results' });
  expect(screen.getByRole('button', { name: 'Show chat' })).toBeInTheDocument();
  expect(order()).toEqual(['sidebar', 'main', 'drawer']);
  // A positive tabindex would pull an element out of that order.
  expect(tabStops().filter((element) => Number(element.getAttribute('tabindex') ?? 0) > 0)).toEqual(
    [],
  );
});

describe('every button has an accessible name', () => {
  it('on the setup view with the plan card', async () => {
    frame('demo-setup');
    await screen.findByRole('button', { name: 'Approve plan' });
    expect(unnamedButtons()).toEqual([]);
  });
  it('on the sample card and the start card', async () => {
    frame('demo-sample');
    await screen.findByRole('button', { name: 'Approve sample' });
    expect(unnamedButtons()).toEqual([]);
    act(() => setJobState('demo-sample', 'sample_approved'));
    await screen.findByRole('button', { name: 'Start full run' });
    expect(unnamedButtons()).toEqual([]);
  });
  it('on the run view, with the chat drawer open and hidden', async () => {
    frame('demo-run');
    await screen.findByText(/of 8 leads read/);
    expect(unnamedButtons()).toEqual([]);
    // The drawer's arrow buttons are named in words and keep their visible text.
    const hide = screen.getByRole('button', { name: 'Hide chat' });
    expect(hide).toHaveTextContent('Hide ›');
    fireEvent.click(hide);
    const show = screen.getByRole('button', { name: 'Show chat' });
    expect(show).toHaveTextContent('‹ Chat');
    expect(unnamedButtons()).toEqual([]);
  });
  it('on the run banners that have buttons', async () => {
    for (const changes of [
      { state: 'paused', pauseReason: 'credit', resumeAt: null },
      { state: 'paused', pauseReason: 'auth', resumeAt: null },
      { state: 'stalled', stall: { reason: 'pull_failed', text: 'Synthetic stall.' } },
    ]) {
      mockBridge.setResponse('job/snapshot', async () => ({ ...running, ...changes }) as never);
      const view = render(<RunView id="job-one" />);
      await screen.findByRole('status');
      expect(unnamedButtons()).toEqual([]);
      view.unmount();
    }
  });
  it('on the done view and in the rebuild dialog', async () => {
    frame('demo-done');
    const rebuild = await screen.findByRole('button', { name: 'Rebuild with new rules' });
    await screen.findByRole('button', { name: 'Open results' });
    expect(unnamedButtons()).toEqual([]);
    fireEvent.click(rebuild);
    const dialog = await screen.findByRole('dialog');
    // The × closes the dialog and says so.
    expect(within(dialog).getByRole('button', { name: 'Close' })).toHaveTextContent('×');
    expect(unnamedButtons()).toEqual([]);
  });
  it('in Settings and the model change dialog', async () => {
    settingsOpen.value = true;
    render(<SettingsView />);
    await screen.findByText('claude-sonnet');
    expect(unnamedButtons()).toEqual([]);
    // Two "Change…" buttons: each says which model it changes.
    fireEvent.click(screen.getByRole('button', { name: 'Change ChatGPT model' }));
    await screen.findByRole('dialog', { name: 'Change the ChatGPT model' });
    expect(unnamedButtons()).toEqual([]);
  });
  it('on the first-run checklist and the empty state', async () => {
    setupItems.value = [
      { name: 'uv_python', status: 'ok' },
      { name: 'claude_login', status: 'pending' },
      { name: 'training_off', status: 'manual_pending' },
    ];
    setupRequired.value = true;
    const checklist = render(<SetupChecklist />);
    await screen.findByText('Set up this computer');
    expect(unnamedButtons()).toEqual([]);
    checklist.unmount();
    setupRequired.value = false;
    projects.value = [];
    frame(null);
    await screen.findByText('No projects yet');
    expect(unnamedButtons()).toEqual([]);
  });
});

it('dialogs trap focus and restore it on escape', async () => {
  // The rebuild dialog.
  const done = frame('demo-done');
  const rebuild = await screen.findByRole('button', { name: 'Rebuild with new rules' });
  await screen.findByRole('button', { name: 'Open results' });
  rebuild.focus();
  fireEvent.click(rebuild);
  const dialog = await screen.findByRole('dialog');
  await waitFor(() => expect(dialog).toContainElement(document.activeElement as HTMLElement));
  const buttons = within(dialog)
    .getAllByRole('button')
    .filter((button) => !(button as HTMLButtonElement).disabled);
  buttons.at(-1)!.focus();
  fireEvent.keyDown(document, { key: 'Tab' });
  expect(document.activeElement).toBe(buttons[0]);
  fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
  expect(document.activeElement).toBe(buttons.at(-1));
  fireEvent.keyDown(document, { key: 'Escape' });
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(document.activeElement).toBe(rebuild);
  done.unmount();

  // The model change dialog. A click does not focus a button in every web view, so the
  // opener is not focused here; focus must still come back to it.
  settingsOpen.value = true;
  render(<SettingsView />);
  await screen.findByText('claude-sonnet');
  const change = screen.getByRole('button', { name: 'Change Claude model' });
  fireEvent.click(change);
  const modelDialog = await screen.findByRole('dialog', { name: 'Change the Claude model' });
  const input = within(modelDialog).getByLabelText('New model');
  await waitFor(() => expect(document.activeElement).toBe(input));
  const confirm = within(modelDialog).getByRole('button', { name: 'Change model' });
  confirm.focus();
  fireEvent.keyDown(document, { key: 'Tab' });
  expect(document.activeElement).toBe(input);
  fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
  expect(document.activeElement).toBe(confirm);
  // Focus that somehow got outside comes back in on the next Tab.
  change.focus();
  fireEvent.keyDown(document, { key: 'Tab' });
  expect(modelDialog).toContainElement(document.activeElement as HTMLElement);
  fireEvent.keyDown(document, { key: 'Escape' });
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(document.activeElement).toBe(change);
  expect(mockBridge.calls.some((call) => call.method === 'settings/set')).toBe(false);
});

it('the composer sends on Enter, adds a line on Shift+Enter and keeps focus after sending', async () => {
  mockBridge.setResponse('chat/history', async () => ({ entries: [], more: false }));
  mockBridge.setResponse('chat/send', async () => ({ status: 'completed', project: 'p1' }));
  render(<Chat project="p1" />);
  const box = screen.getByPlaceholderText(/Ask Claude/) as HTMLTextAreaElement;
  box.focus();
  fireEvent.input(box, { target: { value: 'First line' } });
  // Shift+Enter is left to the text box (a new line); nothing is sent.
  expect(fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })).toBe(true);
  expect(mockBridge.calls.some((call) => call.method === 'chat/send')).toBe(false);
  // Enter sends instead of adding a line.
  expect(fireEvent.keyDown(box, { key: 'Enter' })).toBe(false);
  await waitFor(() =>
    expect(mockBridge.calls).toContainEqual({
      method: 'chat/send',
      params: { project: 'p1', text: 'First line' },
    }),
  );
  expect(box).toHaveValue('');
  expect(document.activeElement).toBe(box);
  // Sending with the button also puts focus back in the box, never on "Stop".
  const send = await screen.findByRole('button', { name: 'Send' });
  fireEvent.input(box, { target: { value: 'Second' } });
  send.focus();
  fireEvent.click(send);
  await waitFor(() =>
    expect(mockBridge.calls.filter((call) => call.method === 'chat/send')).toHaveLength(2),
  );
  expect(document.activeElement).toBe(box);
});

it('Enter while an input method is composing picks the characters and sends nothing', async () => {
  mockBridge.setResponse('chat/history', async () => ({ entries: [], more: false }));
  mockBridge.setResponse('chat/send', async () => ({ status: 'completed', project: 'p1' }));
  render(<Chat project="p1" />);
  const box = screen.getByPlaceholderText(/Ask Claude/) as HTMLTextAreaElement;
  box.focus();
  fireEvent.input(box, { target: { value: 'にほんご' } });
  // The input method's Enter: flagged as composing, or (in some browsers) only keyCode 229.
  expect(fireEvent.keyDown(box, { key: 'Enter', isComposing: true })).toBe(true);
  expect(fireEvent.keyDown(box, { key: 'Enter', keyCode: 229 })).toBe(true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(mockBridge.calls.some((call) => call.method === 'chat/send')).toBe(false);
  expect(box).toHaveValue('にほんご');
  // Once the composition has ended, Enter sends.
  expect(fireEvent.keyDown(box, { key: 'Enter' })).toBe(false);
  await waitFor(() =>
    expect(mockBridge.calls).toContainEqual({
      method: 'chat/send',
      params: { project: 'p1', text: 'にほんご' },
    }),
  );
});

it('hiding and showing the chat keeps focus on the toggle', async () => {
  frame('demo-run');
  const hide = await screen.findByRole('button', { name: 'Hide chat' });
  hide.focus();
  fireEvent.click(hide);
  const show = await screen.findByRole('button', { name: 'Show chat' });
  await waitFor(() => expect(document.activeElement).toBe(show));
  fireEvent.click(show);
  await waitFor(() =>
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Hide chat' })),
  );
});

it('the new project form takes focus, and Create moves it to the new project', async () => {
  frame('demo-setup');
  fireEvent.click(screen.getByRole('button', { name: '+ New project' }));
  const title = await screen.findByLabelText('Project title');
  await waitFor(() => expect(document.activeElement).toBe(title));
  fireEvent.input(title, { target: { value: 'Synthetic focus check' } });
  fireEvent.submit(title.closest('form')!);
  const created = await screen.findByRole('button', { name: /Synthetic focus check/ });
  await waitFor(() => expect(document.activeElement).toBe(created));
});

it('settings starts at its heading and gives focus back to the Settings button', async () => {
  selectedProject.value = 'demo-setup';
  render(<App />);
  const open = await screen.findByRole('button', { name: 'Settings' });
  open.focus();
  fireEvent.click(open);
  const heading = await screen.findByRole('heading', { name: 'Settings' });
  await waitFor(() => expect(document.activeElement).toBe(heading));
  fireEvent.click(screen.getByRole('button', { name: 'Back' }));
  await waitFor(() =>
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Settings' })),
  );
});

it('when the next step replaces the view, focus moves to the project heading', async () => {
  frame('demo-setup');
  const approve = await screen.findByRole('button', { name: 'Approve plan' });
  approve.focus();
  // The plan was approved and the job started: the setup view (and its button) goes away.
  act(() => setJobState('demo-setup', 'running'));
  await screen.findByTestId('run-view');
  await waitFor(() =>
    expect(document.activeElement).toBe(
      screen.getByRole('heading', { name: /Discount questions/ }),
    ),
  );
});

it('when a run finishes while focus is in the chat, focus moves to the project heading', async () => {
  frame('demo-run');
  const box = await screen.findByPlaceholderText(/Ask Claude/);
  box.focus();
  // The run finishes: the done view replaces it, and the chat drawer folds to its tab.
  act(() => setJobState('demo-run', 'done'));
  await screen.findByTestId('done-view');
  expect(screen.queryByPlaceholderText(/Ask Claude/)).toBeNull();
  await waitFor(() =>
    expect(document.activeElement).toBe(
      screen.getByRole('heading', { name: /Webinar objections/ }),
    ),
  );
});

it('status pills and step markers carry text, not colour alone', async () => {
  // Pills say the state in words.
  for (const [state, label] of Object.entries(statusLabels)) {
    const pill = render(<Pill state={state} />);
    expect(pill.container).toHaveTextContent(label);
    pill.unmount();
  }
  // Run view steps: the tick and the spinner are hidden from screen readers, the words are not.
  const run = render(<RunView id="job-one" />);
  await screen.findByText(/of 8 leads read/);
  const rows = [...document.querySelectorAll('.step-row')];
  expect(rows.length).toBeGreaterThan(0);
  for (const row of rows) {
    expect(row.querySelector('.step-icon')).toHaveAttribute('aria-hidden', 'true');
    expect(spokenText(row)).toMatch(/^(Done|In progress|Not started): /);
  }
  expect(rows.some((row) => spokenText(row).startsWith('In progress: '))).toBe(true);
  run.unmount();
  // The job rail: the current step is more than a colour.
  const rail = render(<Rail project="demo-run" state="running" />);
  const steps = [...rail.container.querySelectorAll('.steps li')];
  for (const step of steps) {
    expect(step.querySelector('.step-marker')).toHaveAttribute('aria-hidden', 'true');
    expect(spokenText(step)).toMatch(/^(Done|Current step|Not started): /);
  }
  expect(steps.some((step) => spokenText(step).startsWith('Current step: '))).toBe(true);
  rail.unmount();
  // The first-run checklist: each row says whether it is done.
  setupItems.value = [
    { name: 'uv_python', status: 'ok' },
    { name: 'venv', status: 'ok' },
    { name: 'spacy', status: 'failed', detail: 'Synthetic failure' },
  ];
  setupRequired.value = true;
  render(<SetupChecklist />);
  const titles = [...document.querySelectorAll('.check-row strong')].map(
    (title) => title.textContent,
  );
  expect(titles).toContain('Done: App engine installed');
  expect(titles).toContain('Needs attention: Masking tools ready');
  expect(titles).toContain('Not done yet: BigQuery connector found');
});
