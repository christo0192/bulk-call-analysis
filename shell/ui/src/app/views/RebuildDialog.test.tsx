import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/preact';
import { mockBridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import { plan } from '../../mock/fixtures/data';
import { openProjectFile } from '../../engine/shellCommands';
import { RebuildDialog } from './RebuildDialog';

vi.mock('../../engine/shellCommands', () => ({ openProjectFile: vi.fn() }));

beforeEach(() => mockBridge.resetForTest());

const renderDialog = (onClose = vi.fn()) => {
  render(<RebuildDialog job="job-one" plan={plan} onClose={onClose} />);
  return screen.getByRole('dialog');
};

it('lists current rules and removes one', () => {
  const dialog = renderDialog();
  expect(within(dialog).getByText('Answer from the call, not metadata')).toBeInTheDocument();
  expect(within(dialog).getAllByRole('button', { name: 'Remove' })).toHaveLength(2);
  fireEvent.click(within(dialog).getAllByRole('button', { name: 'Remove' })[0]!);
  expect(within(dialog).getAllByRole('button', { name: 'Remove' })).toHaveLength(1);
});

it('adds a bands rule and validates edges and labels', () => {
  const dialog = renderDialog();
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

it('adds an in scope rule with typed values', async () => {
  const dialog = renderDialog();
  fireEvent.click(within(dialog).getByRole('button', { name: 'In scope' }));
  fireEvent.change(within(dialog).getByLabelText('Field'), {
    target: { value: 'answer.asked_discount' },
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
            conditions: [{ field: 'answer.asked_discount', op: 'eq', value: true }],
          }),
        ]),
      },
    }),
  );
});

it('rebuild success shows the new file with open', async () => {
  const dialog = renderDialog();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild' }));
  expect(
    await within(dialog).findByText(/output\/results\.v2\.rebuild1\.xlsx/),
  ).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Open' }));
  expect(openProjectFile).toHaveBeenCalledWith('demo-setup', 'output/results.v2.rebuild1.xlsx');
});

it('invalid rules shows the engine detail', async () => {
  mockBridge.setResponse('job/rebuildRules', async () => {
    throw new EngineError(-32000, 'rules invalid', 'invalid_rules', { detail: 'Edges overlap' });
  });
  const dialog = renderDialog();
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
