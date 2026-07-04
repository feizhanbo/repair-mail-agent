import { Tag } from 'antd';
import {
  parseStatusLabels,
  reviewStatusLabels,
  taskPriorityColors,
  taskStatusLabels,
  ticketStatusColors,
  ticketStatusLabels,
} from '../utils/status';

type StatusTagProps = {
  value?: string | null;
  kind?: 'ticket' | 'parse' | 'task' | 'review' | 'priority' | 'plain';
};

const defaultColors: Record<string, string> = {
  pass: 'green',
  warning: 'gold',
  failed: 'red',
  pending: 'blue',
  success: 'green',
  error: 'red',
  draft: 'blue',
  send_disabled: 'default',
  rejected: 'red',
};

function getLabel(value: string, kind: StatusTagProps['kind']) {
  if (kind === 'ticket') return ticketStatusLabels[value] ?? value;
  if (kind === 'parse') return parseStatusLabels[value] ?? value;
  if (kind === 'task') return taskStatusLabels[value] ?? value;
  if (kind === 'review') return reviewStatusLabels[value] ?? value;
  return value;
}

function getColor(value: string, kind: StatusTagProps['kind']) {
  if (kind === 'ticket') return ticketStatusColors[value] ?? 'default';
  if (kind === 'priority') return taskPriorityColors[value] ?? 'default';
  return defaultColors[value] ?? 'blue';
}

export default function StatusTag({ value, kind = 'plain' }: StatusTagProps) {
  if (!value) {
    return <Tag>-</Tag>;
  }
  return <Tag color={getColor(value, kind)}>{getLabel(value, kind)}</Tag>;
}
