import { Component, type ComponentChildren } from 'preact';

type Props = { children: ComponentChildren; resetKey?: string };

export class ErrorBoundary extends Component<Props, { failed: boolean }> {
  state = { failed: false };

  componentDidUpdate(previousProps: Props) {
    if (this.state.failed && previousProps.resetKey !== this.props.resetKey) {
      this.setState({ failed: false });
    }
  }

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  render() {
    return this.state.failed ? (
      <div class="view-error" role="alert">
        Something went wrong in this view.{' '}
        <button onClick={() => this.setState({ failed: false })}>Reload</button>
      </div>
    ) : (
      this.props.children
    );
  }
}
