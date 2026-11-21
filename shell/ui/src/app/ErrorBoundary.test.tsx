import { render, screen } from '@testing-library/preact';
import { expect, it } from 'vitest';
import { ErrorBoundary } from './ErrorBoundary';

it('error boundary shows a reload panel instead of a blank window', () => {
  const Broken = () => {
    throw new Error('broken');
  };
  render(
    <ErrorBoundary>
      <Broken />
    </ErrorBoundary>,
  );
  expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong in this view. Reload');
});
