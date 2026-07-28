import { CheckOutlined, CopyOutlined } from '@ant-design/icons';
import { message, Typography } from 'antd';
import { useState, useCallback } from 'react';

type Props = {
  value: string;
  displayText?: string;
  showIcon?: boolean;
  style?: React.CSSProperties;
};

export default function CopyableField({ value, displayText, showIcon = true, style }: Props) {
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      message.success('已复制');
      setTimeout(() => setCopied(false), 1500);
    } catch {
      message.error('复制失败');
    }
  }, [value]);

  return (
    <span style={{ cursor: 'pointer', ...style }} onClick={handleCopy} title="点击复制">
      <Typography.Text>{displayText ?? value}</Typography.Text>
      {showIcon && (
        copied
          ? <CheckOutlined style={{ marginLeft: 4, color: '#52c41a' }} />
          : <CopyOutlined style={{ marginLeft: 4, opacity: 0.4 }} />
      )}
    </span>
  );
}
