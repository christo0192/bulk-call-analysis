import { Component, type ComponentChildren } from 'preact';

export class ErrorBoundary extends Component<{ children: ComponentChildren }, { failed: boolean }> {
  state = { failed: false };

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
