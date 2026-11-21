import { Component, type ComponentChildren } from 'preact';

export class ErrorBoundary extends Component<{ children: ComponentChildren }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  render() {
    return this.state.failed ? (
      <div class="view-error" role="alert">
        Something went wrong in this view. Reload
      </div>
    ) : (
      this.props.children
    );
  }
}
