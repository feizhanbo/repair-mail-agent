import { Alert, Button } from 'antd';

type Props = {
  message: string;
  onRetry?: () => void;
};

export default function ErrorResult({ message, onRetry }: Props) {
  return (
    <Alert
      type="error"
      showIcon
      message="加载失败"
      description={message}
      action={onRetry ? <Button size="small" onClick={onRetry}>重试</Button> : undefined}
    />
  );
}
