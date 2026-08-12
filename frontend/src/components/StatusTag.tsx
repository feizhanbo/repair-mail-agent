import { Tag } from 'antd';
import {
  parseStatusLabels,
  reviewStatusLabels,
  rmaStatusLabels,
  routeStatusLabels,
  sapStatusLabels,
  sendStatusLabels,
  taskPriorityColors,
  taskStatusColors,
  taskStatusLabels,
  ticketStatusColors,
  ticketStatusLabels,
  validationStatusLabels,
} from '../utils/status';

type StatusTagProps = {
  value?: string | null;
  kind?: 'ticket' | 'parse' | 'task' | 'review' | 'priority' | 'send' | 'route' | 'sap' | 'rma' | 'validation' | 'plain';
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
  send_uncertain: 'orange',
  assignment_failed: 'red',
  skipped: 'default',
  rejected: 'red',
  auto_applied: 'green',
  manually_applied: 'green',
  partially_applied: 'gold',
  needs_manual_review: 'orange',
  candidate_only: 'default',
  auto_skipped: 'default',
  engineering_reference_stored: 'default',
  submitting: 'processing',
  submitted: 'cyan',
  accepted: 'cyan',
  waiting_sap_result: 'blue',
  waiting_rma: 'blue',
  submit_unknown: 'orange',
  submit_failed: 'red',
  awaiting_approval: 'orange',
  applying: 'processing',
  succeeded: 'green',
  rma_received: 'green',
  timed_out: 'orange',
  manual_review: 'orange',
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
  submitting: '提交中',
  submitted: '已提交',
  accepted: '已受理',
  waiting_sap_result: '等待 SAP 结果',
  waiting_rma: '等待 RMA',
  submit_unknown: '提交结果未知',
  submit_failed: '提交失败',
  awaiting_approval: '待管理员确认',
  applying: '应用中',
  succeeded: '已生效',
  rma_received: 'RMA 已回填',
  timed_out: '回填超时',
  manual_review: '人工复核',
};

function getLabel(value: string, kind: StatusTagProps['kind']) {
  if (kind === 'ticket') return ticketStatusLabels[value] ?? value;
  if (kind === 'parse') return parseStatusLabels[value] ?? value;
  if (kind === 'task') return taskStatusLabels[value] ?? value;
  if (kind === 'review') return reviewStatusLabels[value] ?? value;
  if (kind === 'send') return sendStatusLabels[value] ?? value;
  if (kind === 'route') return routeStatusLabels[value] ?? value;
  if (kind === 'sap') return sapStatusLabels[value] ?? value;
  if (kind === 'rma') return rmaStatusLabels[value] ?? value;
  if (kind === 'validation') return validationStatusLabels[value] ?? value;
  return defaultLabels[value] ?? value;
}

function getColor(value: string, kind: StatusTagProps['kind']) {
  if (kind === 'ticket') return ticketStatusColors[value] ?? 'default';
  if (kind === 'task') return taskStatusColors[value] ?? 'blue';
  if (kind === 'priority') return taskPriorityColors[value] ?? 'default';
  if (kind === 'send') return defaultColors[value] ?? 'blue';
  return defaultColors[value] ?? 'blue';
}

export default function StatusTag({ value, kind = 'plain' }: StatusTagProps) {
  if (!value) {
    return <Tag>-</Tag>;
  }
  return <Tag color={getColor(value, kind)}>{getLabel(value, kind)}</Tag>;
}
