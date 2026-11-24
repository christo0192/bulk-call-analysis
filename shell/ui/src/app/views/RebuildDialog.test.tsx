import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/preact';
import { mockBridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import { plan } from '../../mock/fixtures/data';
import { openProjectFile } from '../../engine/shellCommands';
import { RebuildDialog } from './RebuildDialog';
import type { PlanCard } from '../../engine/types';

// The shell command resolves to null on success, like the real Tauri command.
vi.mock('../../engine/shellCommands', () => ({ openProjectFile: vi.fn(async () => null) }));

beforeEach(() => mockBridge.resetForTest());

const recordedPlan: PlanCard = {
  ...plan,
  job: 'job-one',
  project: 'project-one',
  outputColumns: [{ name: 'answer', type: 'string' }],
  rules: [],
};
const planWithRules: PlanCard = {
  ...recordedPlan,
  rules: [
    {
      kind: 'threshold_group',
      summary: 'Answer from the call, not metadata',
      definition: {
        kind: 'threshold_group',
        name: 'Answer from the call, not metadata',
        field: 'answer.answer',
        edges: [1],
        labels: ['low', 'high'],
      },
    },
    {
      kind: 'review_flag',
      summary: 'Include a short masked quote for every answer',
      definition: {
        kind: 'review_flag',
        name: 'Review answer',
        match: 'all',
        conditions: [{ field: 'answer.answer', op: 'contains', value: 'urgent' }],
      },
    },
  ],
};
const planWithNumberColumn: PlanCard = {
  ...recordedPlan,
  outputColumns: [{ name: 'score', type: 'number' }],
};

const renderDialog = (onClose = vi.fn(), currentPlan = recordedPlan) => {
  render(<RebuildDialog job="job-one" plan={currentPlan} onClose={onClose} />);
  return screen.getByRole('dialog');
};

it('lists current rules and removes one', () => {
  const dialog = renderDialog(vi.fn(), planWithRules);
  expect(within(dialog).getByText('Answer from the call, not metadata')).toBeInTheDocument();
  expect(within(dialog).getAllByRole('button', { name: 'Remove' })).toHaveLength(2);
  fireEvent.click(within(dialog).getAllByRole('button', { name: 'Remove' })[0]!);
  expect(within(dialog).getAllByRole('button', { name: 'Remove' })).toHaveLength(1);
});

it('adds a bands rule and validates edges and labels', () => {
  const dialog = renderDialog(vi.fn(), planWithNumberColumn);
  fireEvent.input(within(dialog).getByLabelText('Edges'), { target: { value: '10, 5' } });
  fireEvent.input(within(dialog).getByLabelText('Labels'), {
    target: { value: 'low, high, very high' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  expect(within(dialog).getByRole('alert')).toHaveTextContent('strictly increasing');
  fireEvent.input(within(dialog).getByLabelText('Edges'), { target: { value: '5, 10' } });
  fireEvent.input(within(dialog).getByLabelText('Labels'), {
    target: { value: 'low, medium, high' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  expect(mockBridge.calls).toContainEqual(
    expect.objectContaining({
      method: 'job/rebuildRules',
      params: expect.objectContaining({
        rules: expect.arrayContaining([
          expect.objectContaining({
            kind: 'threshold_group',
            edges: [5, 10],
            labels: ['low', 'medium', 'high'],
          }),
        ]),
      }),
    }),
  );
});

it('bands explain when there is no number column', () => {
  const dialog = renderDialog();
  expect(
    within(dialog).getByText('Bands need a number column; this job has none.'),
  ).toBeInTheDocument();
  expect(within(dialog).getByRole('button', { name: 'Add rule' })).toBeDisabled();
});

it('adds an in scope rule with typed values', async () => {
  const dialog = renderDialog();
  fireEvent.click(within(dialog).getByRole('button', { name: 'In scope' }));
  fireEvent.change(within(dialog).getByLabelText('Field'), {
    target: { value: 'answer.answer' },
  });
  fireEvent.change(within(dialog).getByLabelText('Operator'), { target: { value: 'eq' } });
  fireEvent.input(within(dialog).getByLabelText('Value'), { target: { value: 'true' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  await vi.waitFor(() =>
    expect(mockBridge.calls).toContainEqual({
      method: 'job/rebuildRules',
      params: {
        job: 'job-one',
        rules: expect.arrayContaining([
          expect.objectContaining({
            kind: 'in_scope',
            conditions: [{ field: 'answer.answer', op: 'eq', value: 'true' }],
          }),
        ]),
      },
    }),
  );
});

it('rebuild success shows the new file with open', async () => {
  const dialog = renderDialog(vi.fn(), planWithRules);
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  // The dialog shows the file name only and opens by the project-relative path.
  expect(
    await within(dialog).findByText(/Rebuilt: results\.v2\.rebuild1\.xlsx/),
  ).toBeInTheDocument();
  expect(within(dialog).queryByText(/jobs\//)).toBeNull();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Open' }));
  expect(openProjectFile).toHaveBeenCalledWith(
    'project-one',
    'jobs/job-one/output/results.v2.rebuild1.xlsx',
  );
});

it('invalid rules shows the engine detail', async () => {
  mockBridge.setResponse('job/rebuildRules', async () => {
    throw new EngineError(-32000, 'rules invalid', 'invalid_rules', { detail: 'Edges overlap' });
  });
  const dialog = renderDialog(vi.fn(), planWithRules);
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('Edges overlap');
});

it('escape closes and returns focus', () => {
  const onClose = vi.fn();
  renderDialog(onClose);
  fireEvent.keyDown(document, { key: 'Escape' });
  expect(onClose).toHaveBeenCalledOnce();
});

it('focus is trapped inside the dialog', () => {
  const dialog = renderDialog();
  const buttons = within(dialog).getAllByRole('button');
  const first = buttons[0]!;
  const last = buttons[buttons.length - 1]!;
  last.focus();
  fireEvent.keyDown(document, { key: 'Tab' });
  expect(document.activeElement).toBe(first);
  first.focus();
  fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
  expect(document.activeElement).toBe(last);
});
