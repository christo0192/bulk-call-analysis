import { expect, it } from 'vitest';
import { render, screen } from '@testing-library/preact';
import { Pill } from './Pill';
const cases = [
  ['planned', 'Setting up', 'setup'],
  ['plan_approved', 'Preparing', 'setup'],
  ['sample_ready', 'Sample ready', 'setup'],
  ['running', 'Reading', 'active'],
  ['paused', 'Paused', 'amber'],
  ['stalled', 'Stalled', 'danger'],
  ['done', 'Done', 'active'],
  ['done_with_issues', 'Done with issues', 'amber'],
  ['failed', 'Failed', 'danger'],
  ['cancelled', 'Cancelled', 'setup'],
] as const;
it.each(cases)('renders %s as %s with %s class', (state, label, className) => {
  render(<Pill state={state} />);
  expect(screen.getByText(label)).toHaveClass(className);
});
