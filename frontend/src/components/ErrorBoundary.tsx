import React from "react";

interface Props {
  children: React.ReactNode;
  fallback?: (error: Error, reset: () => void) => React.ReactNode;
}
interface State {
  error: Error | null;
}

/**
 * Catches render-time exceptions in any descendant view so a single bad response
 * (e.g. a backend schema regression) degrades to a recoverable card instead of a
 * blank white screen. The user can retry without reloading the whole app.
 */
export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("[ErrorBoundary] Uncaught UI error:", error, info.componentStack);
  }

  reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      if (this.props.fallback) return this.props.fallback(this.state.error, this.reset);
      return (
        <div className="card error" style={{ margin: 24, padding: 20 }}>
          <h3>界面出现未捕获的错误</h3>
          <p className="muted" style={{ marginTop: 6 }}>
            {this.state.error.message}
          </p>
          <button className="btn primary" style={{ marginTop: 10 }} onClick={this.reset}>
            重试
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
