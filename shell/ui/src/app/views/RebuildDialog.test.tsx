import { beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/preact';
import { mockBridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import recordedPlanFixture from '../../mock/fixtures/engine/job-plan.json';
import { openProjectFile } from '../../engine/shellCommands';
import { RebuildDialog } from './RebuildDialog';
import type { PlanCard } from '../../engine/types';

// The shell command resolves to null on success, like the real Tauri command.
vi.mock('../../engine/shellCommands', () => ({ openProjectFile: vi.fn(async () => null) }));

beforeEach(() => mockBridge.resetForTest());

const recordedPlan = {
  ...recordedPlanFixture,
  job: 'job-one',
  project: 'project-one',
  rules: [],
} as unknown as PlanCard;
const planWithRules: PlanCard = {
  ...recordedPlan,
  outputColumns: [
    { name: 'asked_discount', type: 'boolean' },
    { name: 'discount_percent', type: 'number' },
    { name: 'reason', type: 'string' },
  ],
  rules: [
    {
      kind: 'in_scope',
      summary: 'Asked for a discount',
      definition: {
        kind: 'in_scope',
        match: 'all',
        conditions: [{ field: 'answer.asked_discount', op: 'eq', value: true }],
      },
    },
    {
      kind: 'review_flag',
      summary: 'Discount reason is present',
      definition: {
        kind: 'review_flag',
        name: 'discount reason present',
        match: 'all',
        conditions: [{ field: 'answer.reason', op: 'not_null' }],
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

it('removing a rule removes it from the payload', async () => {
  const dialog = renderDialog(vi.fn(), planWithRules);
  expect(within(dialog).getByText('Asked for a discount')).toBeInTheDocument();
  expect(within(dialog).getAllByRole('button', { name: 'Remove' })).toHaveLength(2);
  fireEvent.click(within(dialog).getAllByRole('button', { name: 'Remove' })[0]!);
  expect(within(dialog).getAllByRole('button', { name: 'Remove' })).toHaveLength(1);
  // One rule is left, so the rebuild goes straight through with only that rule.
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  expect(within(dialog).queryByText(/removes every rule/)).toBeNull();
  await vi.waitFor(() =>
    expect(mockBridge.calls).toContainEqual({
      method: 'job/rebuildRules',
      params: { job: 'job-one', rules: [planWithRules.rules[1]!.definition] },
    }),
  );
});

it('empty list asks first', () => {
  const dialog = renderDialog();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  expect(within(dialog).getByRole('alert')).toHaveTextContent('This removes every rule');
  expect(mockBridge.calls).not.toContainEqual(
    expect.objectContaining({ method: 'job/rebuildRules' }),
  );
});

it('sends kept and new rules together', async () => {
  const dialog = renderDialog(vi.fn(), planWithRules);
  fireEvent.input(within(dialog).getByLabelText('Edges'), { target: { value: '10, 20' } });
  fireEvent.input(within(dialog).getByLabelText('Labels'), {
    target: { value: 'low, medium, high' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  await vi.waitFor(() => {
    const call = mockBridge.calls.find((item) => item.method === 'job/rebuildRules');
    expect(call?.params).toMatchObject({ job: 'job-one' });
    expect((call?.params as { rules: unknown[] }).rules).toHaveLength(3);
  });
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

it('each operator and type sends the form the engine accepts', async () => {
  const currentPlan: PlanCard = {
    ...recordedPlan,
    outputColumns: [
      { name: 'count', type: 'integer' },
      { name: 'score', type: 'number' },
      { name: 'flag', type: 'boolean' },
      { name: 'label', type: 'string' },
    ],
  };
  const dialog = renderDialog(vi.fn(), currentPlan);
  // One in-scope rule (the engine allows one), then review flags for the other operators.
  fireEvent.click(within(dialog).getByRole('button', { name: 'In scope' }));
  fireEvent.change(within(dialog).getByLabelText('Field'), { target: { value: 'answer.count' } });
  fireEvent.change(within(dialog).getByLabelText('Operator'), { target: { value: 'gt' } });
  fireEvent.input(within(dialog).getByLabelText('Value'), { target: { value: '3' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  // The dialog leaves the in-scope form by itself once an in-scope rule exists.
  expect(within(dialog).getByRole('button', { name: 'In scope' })).toBeDisabled();
  fireEvent.change(within(dialog).getByLabelText('Field'), { target: { value: 'answer.score' } });
  fireEvent.change(within(dialog).getByLabelText('Operator'), { target: { value: 'in' } });
  fireEvent.input(within(dialog).getByLabelText('Value'), { target: { value: '1.5, 2.5' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  fireEvent.change(within(dialog).getByLabelText('Field'), { target: { value: 'answer.flag' } });
  fireEvent.change(within(dialog).getByLabelText('Operator'), { target: { value: 'eq' } });
  fireEvent.change(within(dialog).getByLabelText('Value'), { target: { value: 'true' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  fireEvent.change(within(dialog).getByLabelText('Field'), { target: { value: 'answer.label' } });
  fireEvent.change(within(dialog).getByLabelText('Operator'), { target: { value: 'contains' } });
  fireEvent.input(within(dialog).getByLabelText('Value'), { target: { value: 'urgent' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  await vi.waitFor(() => {
    const call = mockBridge.calls.find((item) => item.method === 'job/rebuildRules');
    // Exact payload; the orchestrator checked these four definitions with the engine's own
    // _rule and validate_rules (bca.store.spec, bca.outputs.rules).
    expect(call?.params).toEqual({
      job: 'job-one',
      rules: [
        {
          kind: 'in_scope',
          match: 'all',
          conditions: [{ field: 'answer.count', op: 'gt', value: 3 }],
        },
        {
          kind: 'review_flag',
          name: 'review flag 1',
          match: 'all',
          conditions: [{ field: 'answer.score', op: 'in', value: [1.5, 2.5] }],
        },
        {
          kind: 'review_flag',
          name: 'review flag 2',
          match: 'all',
          conditions: [{ field: 'answer.flag', op: 'eq', value: true }],
        },
        {
          kind: 'review_flag',
          name: 'review flag 3',
          match: 'all',
          conditions: [{ field: 'answer.label', op: 'contains', value: 'urgent' }],
        },
      ],
    });
  });
});

it('empty and bad values are refused', () => {
  const currentPlan: PlanCard = {
    ...recordedPlan,
    outputColumns: [
      { name: 'count', type: 'integer' },
      { name: 'flag', type: 'boolean' },
    ],
  };
  const dialog = renderDialog(vi.fn(), currentPlan);
  fireEvent.click(within(dialog).getByRole('button', { name: 'In scope' }));
  fireEvent.change(within(dialog).getByLabelText('Field'), { target: { value: 'answer.count' } });
  fireEvent.change(within(dialog).getByLabelText('Operator'), { target: { value: 'eq' } });
  fireEvent.input(within(dialog).getByLabelText('Value'), { target: { value: '' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  expect(within(dialog).getByRole('alert')).toHaveTextContent('Enter a value');
  fireEvent.input(within(dialog).getByLabelText('Value'), { target: { value: '3.7' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  expect(within(dialog).getByRole('alert')).toHaveTextContent('Whole numbers only');
  fireEvent.change(within(dialog).getByLabelText('Field'), { target: { value: 'answer.flag' } });
  fireEvent.change(within(dialog).getByLabelText('Value'), { target: { value: '' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  expect(within(dialog).getByRole('alert')).toHaveTextContent('Choose Yes or No');
  expect(mockBridge.calls).not.toContainEqual(
    expect.objectContaining({ method: 'job/rebuildRules' }),
  );
});

it('kept is_null rules are sent without a value', async () => {
  const currentPlan: PlanCard = {
    ...recordedPlan,
    outputColumns: [{ name: 'reason', type: 'string' }],
    rules: [
      {
        kind: 'review_flag',
        summary: 'Missing reason',
        definition: {
          kind: 'review_flag',
          name: 'missing reason',
          match: 'all',
          conditions: [{ field: 'answer.reason', op: 'is_null', value: 'should be removed' }],
        },
      },
    ],
  };
  const dialog = renderDialog(vi.fn(), currentPlan);
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  await vi.waitFor(() =>
    expect(mockBridge.calls).toContainEqual({
      method: 'job/rebuildRules',
      params: {
        job: 'job-one',
        rules: [
          {
            kind: 'review_flag',
            name: 'missing reason',
            match: 'all',
            conditions: [{ field: 'answer.reason', op: 'is_null' }],
          },
        ],
      },
    }),
  );
});

it('success shows the name only and opens by path', async () => {
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

it('second in scope is blocked', async () => {
  // Kept in-scope rule: the kind is disabled.
  const kept = renderDialog(vi.fn(), planWithRules);
  expect(within(kept).getByRole('button', { name: 'In scope' })).toBeDisabled();
  cleanup();
  // A new in-scope rule: a second one can't be added, and one reaches the engine.
  const currentPlan: PlanCard = {
    ...recordedPlan,
    outputColumns: [
      { name: 'count', type: 'integer' },
      { name: 'label', type: 'string' },
    ],
  };
  const dialog = renderDialog(vi.fn(), currentPlan);
  fireEvent.click(within(dialog).getByRole('button', { name: 'In scope' }));
  fireEvent.change(within(dialog).getByLabelText('Field'), { target: { value: 'answer.count' } });
  fireEvent.change(within(dialog).getByLabelText('Operator'), { target: { value: 'gt' } });
  fireEvent.input(within(dialog).getByLabelText('Value'), { target: { value: '3' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  expect(within(dialog).getByRole('button', { name: 'In scope' })).toBeDisabled();
  fireEvent.change(within(dialog).getByLabelText('Field'), { target: { value: 'answer.label' } });
  fireEvent.change(within(dialog).getByLabelText('Operator'), { target: { value: 'eq' } });
  fireEvent.input(within(dialog).getByLabelText('Value'), { target: { value: 'x' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  await vi.waitFor(() => {
    const call = mockBridge.calls.find((item) => item.method === 'job/rebuildRules');
    const rules = (call?.params as { rules: { kind: string }[] }).rules;
    expect(rules.map((rule) => rule.kind)).toEqual(['in_scope', 'review_flag']);
  });
});

it('a comma inside a single value is kept, and number lists keep every item', async () => {
  const currentPlan: PlanCard = {
    ...recordedPlan,
    outputColumns: [
      { name: 'reason', type: 'string' },
      { name: 'score', type: 'number' },
      { name: 'count', type: 'integer' },
    ],
  };
  const dialog = renderDialog(vi.fn(), currentPlan);
  fireEvent.click(within(dialog).getByRole('button', { name: 'Review flag' }));
  fireEvent.change(within(dialog).getByLabelText('Field'), { target: { value: 'answer.reason' } });
  fireEvent.change(within(dialog).getByLabelText('Operator'), { target: { value: 'eq' } });
  fireEvent.input(within(dialog).getByLabelText('Value'), {
    target: { value: 'too expensive, will think' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  // "1,5" is not a number here (no decimal commas): refused, nothing added.
  fireEvent.change(within(dialog).getByLabelText('Field'), { target: { value: 'answer.score' } });
  fireEvent.change(within(dialog).getByLabelText('Operator'), { target: { value: 'eq' } });
  fireEvent.input(within(dialog).getByLabelText('Value'), { target: { value: '1,5' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  expect(within(dialog).getByRole('alert')).toHaveTextContent('Enter a number, like 12 or 12.5');
  fireEvent.change(within(dialog).getByLabelText('Field'), { target: { value: 'answer.count' } });
  fireEvent.change(within(dialog).getByLabelText('Operator'), { target: { value: 'in' } });
  fireEvent.input(within(dialog).getByLabelText('Value'), { target: { value: '1, 2' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  // New rules show their values, so a mistyped value is visible before Rebuild.
  expect(within(dialog).getByText(/reason is "too expensive, will think"/)).toBeInTheDocument();
  expect(within(dialog).getByText(/count is one of 1, 2/)).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  await vi.waitFor(() => {
    const call = mockBridge.calls.find((item) => item.method === 'job/rebuildRules');
    expect(call?.params).toEqual({
      job: 'job-one',
      rules: [
        {
          kind: 'review_flag',
          name: 'review flag 1',
          match: 'all',
          conditions: [{ field: 'answer.reason', op: 'eq', value: 'too expensive, will think' }],
        },
        {
          kind: 'review_flag',
          name: 'review flag 2',
          match: 'all',
          conditions: [{ field: 'answer.count', op: 'in', value: [1, 2] }],
        },
      ],
    });
  });
});

it('bands refuse empty and non numeric edges', () => {
  const dialog = renderDialog(vi.fn(), planWithNumberColumn);
  fireEvent.input(within(dialog).getByLabelText('Edges'), { target: { value: '' } });
  fireEvent.input(within(dialog).getByLabelText('Labels'), { target: { value: 'low' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  expect(within(dialog).getByRole('alert')).toHaveTextContent('not a finite number');
  fireEvent.input(within(dialog).getByLabelText('Edges'), { target: { value: 'ten' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  expect(within(dialog).getByRole('alert')).toHaveTextContent('not a finite number');
});

it('double click sends one call', async () => {
  const dialog = renderDialog(vi.fn(), planWithRules);
  const rebuild = within(dialog).getByRole('button', { name: 'Rebuild' });
  fireEvent.click(rebuild);
  fireEvent.click(rebuild);
  await vi.waitFor(() =>
    expect(mockBridge.calls.filter((call) => call.method === 'job/rebuildRules')).toHaveLength(1),
  );
});

it('rejected rule can be removed and rebuilt', async () => {
  mockBridge.setResponse('job/rebuildRules', async () => {
    throw new EngineError(-32000, 'invalid', 'invalid_rules', { detail: 'Bad rule' });
  });
  const dialog = renderDialog(vi.fn(), planWithRules);
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  await within(dialog).findByText('Bad rule');
  fireEvent.click(within(dialog).getAllByRole('button', { name: 'Remove' })[0]!);
  mockBridge.setResponse('job/rebuildRules', async () => ({
    job: 'job-one',
    name: 'results.xlsx',
    path: 'jobs/job-one/output/results.xlsx',
    rebuild: 1,
  }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  expect(await within(dialog).findByText(/Rebuilt: results\.xlsx/)).toBeInTheDocument();
});

it('escape closes and returns focus', () => {
  const onClose = vi.fn();
  const opener = document.createElement('button');
  opener.textContent = 'open';
  document.body.append(opener);
  opener.focus();
  let view: ReturnType<typeof render>;
  view = render(
    <RebuildDialog
      job="job-one"
      plan={recordedPlan}
      onClose={() => {
        onClose();
        view.unmount();
      }}
    />,
  );
  fireEvent.keyDown(document, { key: 'Escape' });
  expect(onClose).toHaveBeenCalledOnce();
  expect(document.activeElement).toBe(opener);
  opener.remove();
});

it('re-render does not move focus', () => {
  const onClose = vi.fn();
  const view = render(<RebuildDialog job="job-one" plan={recordedPlan} onClose={onClose} />);
  const input = view.getByLabelText('Name');
  input.focus();
  view.rerender(<RebuildDialog job="job-one" plan={recordedPlan} onClose={onClose} />);
  expect(document.activeElement).toBe(input);
});

it('closes when the job changes', () => {
  const onClose = vi.fn();
  const view = render(<RebuildDialog job="job-one" plan={recordedPlan} onClose={onClose} />);
  view.rerender(<RebuildDialog job="job-two" plan={recordedPlan} onClose={onClose} />);
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

it('enum list options are sent exactly as listed', async () => {
  const currentPlan: PlanCard = {
    ...recordedPlan,
    outputColumns: [
      {
        name: 'objection',
        type: 'string',
        values: ['too expensive, will think', 'not now', ' padded '],
      },
    ],
  };
  const dialog = renderDialog(vi.fn(), currentPlan);
  fireEvent.click(within(dialog).getByRole('button', { name: 'Review flag' }));
  fireEvent.change(within(dialog).getByLabelText('Field'), {
    target: { value: 'answer.objection' },
  });
  fireEvent.change(within(dialog).getByLabelText('Operator'), { target: { value: 'in' } });
  // Nothing chosen yet: refused.
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  expect(within(dialog).getByRole('alert')).toHaveTextContent('Choose at least one option');
  const select = within(dialog).getByLabelText('Value') as HTMLSelectElement;
  expect(select.multiple).toBe(true);
  for (const option of Array.from(select.options))
    option.selected = option.value === 'too expensive, will think' || option.value === ' padded ';
  fireEvent.change(select);
  fireEvent.click(within(dialog).getByRole('button', { name: 'Add rule' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  await vi.waitFor(() => {
    const call = mockBridge.calls.find((item) => item.method === 'job/rebuildRules');
    expect(call?.params).toEqual({
      job: 'job-one',
      rules: [
        {
          kind: 'review_flag',
          name: 'review flag 1',
          match: 'all',
          // Not split at the comma, not trimmed.
          conditions: [
            {
              field: 'answer.objection',
              op: 'in',
              value: ['too expensive, will think', ' padded '],
            },
          ],
        },
      ],
    });
  });
});

it('number values use a text box (a number box drops the commas of a list)', () => {
  const currentPlan: PlanCard = {
    ...recordedPlan,
    outputColumns: [
      { name: 'count', type: 'integer' },
      { name: 'score', type: 'number' },
    ],
  };
  const dialog = renderDialog(vi.fn(), currentPlan);
  fireEvent.click(within(dialog).getByRole('button', { name: 'Review flag' }));
  for (const field of ['answer.count', 'answer.score']) {
    fireEvent.change(within(dialog).getByLabelText('Field'), { target: { value: field } });
    fireEvent.change(within(dialog).getByLabelText('Operator'), { target: { value: 'in' } });
    expect((within(dialog).getByLabelText('Value') as HTMLInputElement).type).toBe('text');
  }
});
