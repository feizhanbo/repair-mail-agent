import { Result, Button } from 'antd';
import { Component, type ReactNode } from 'react';

type Props = { children: ReactNode };
type State = { hasError: boolean; error: Error | null };

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      return (
        <Result
          status="error"
          title="页面发生错误"
          subTitle={this.state.error?.message || '未知错误'}
          extra={[<Button type="primary" key="refresh" onClick={() => window.location.reload()}>刷新页面</Button>]}
        />
      );
    }
    return this.props.children;
  }
}
