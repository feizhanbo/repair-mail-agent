import { Typography } from 'antd';
import type { JsonRecord } from '../types/api';

type JsonBlockProps = {
  value?: JsonRecord | unknown[] | null;
};

export default function JsonBlock({ value }: JsonBlockProps) {
  if (!value || (typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === 0)) {
    return <Typography.Text type="secondary">-</Typography.Text>;
  }
  return <pre className="json-block">{JSON.stringify(value, null, 2)}</pre>;
}
