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
  pending_review: 'blue',
  approved_pending_send: 'gold',
  sending: 'cyan',
  auto_sending: 'cyan',
  sent: 'green',
  send_failed: 'red',
  send_disabled: 'default',
  rejected: 'red',
  auto_applied: 'green',
  manually_applied: 'green',
  partially_applied: 'gold',
  needs_manual_review: 'orange',
  candidate_only: 'default',
  auto_skipped: 'default',
};

const defaultLabels: Record<string, string> = {
  pending: '待处理',
  success: '成功',
  draft: '草稿',
  pending_review: '待人工确认',
  approved_pending_send: '已确认待发送',
  sending: '发送中',
  auto_sending: '自动发送中',
  sent: '已发送',
  send_failed: '发送失败',
  send_disabled: '未启用发送',
  rejected: '已拒绝',
  auto_applied: '自动应用',
  manually_applied: '人工应用',
  partially_applied: '部分应用',
  needs_manual_review: '需人工复核',
  candidate_only: '规则候选',
  auto_skipped: '已跳过',
};

function getLabel(value: string, kind: StatusTagProps['kind']) {
  if (kind === 'ticket') return ticketStatusLabels[value] ?? value;
  if (kind === 'parse') return parseStatusLabels[value] ?? value;
  if (kind === 'task') return taskStatusLabels[value] ?? value;
  if (kind === 'review') return reviewStatusLabels[value] ?? value;
  return defaultLabels[value] ?? value;
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
