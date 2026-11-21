import { fireEvent, render, screen } from '@testing-library/preact';
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

it('a crashing view keeps the sidebar and offers a working Reload button', () => {
  let broken = true;
  const View = () => {
    if (broken) throw new Error('broken');
    return <main>Recovered</main>;
  };
  render(
    <>
      <nav>Sidebar</nav>
      <ErrorBoundary>
        <View />
      </ErrorBoundary>
    </>,
  );
  expect(screen.getByText('Sidebar')).toBeInTheDocument();
  broken = false;
  fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
  expect(screen.getByText('Recovered')).toBeInTheDocument();
});

it('the error boundary resets when the project changes', () => {
  let broken = true;
  const View = ({ project }: { project: string }) => {
    if (broken) throw new Error(project);
    return <main>{project}</main>;
  };
  const view = render(
    <ErrorBoundary resetKey="project-one">
      <View project="project-one" />
    </ErrorBoundary>,
  );
  broken = false;
  view.rerender(
    <ErrorBoundary resetKey="project-two">
      <View project="project-two" />
    </ErrorBoundary>,
  );
  expect(screen.getByText('project-two')).toBeInTheDocument();
});
