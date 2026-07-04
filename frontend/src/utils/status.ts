export const ticketStatusLabels: Record<string, string> = {
  new_email: '邮件已入库',
  parsed: '已解析',
  need_customer_info: '待客户补充',
  auto_replied: '已追问',
  manual_review: '人工复核',
  ready_for_export: '可导出',
  error: '异常',
  closed: '已关闭',
};

export const ticketStatusColors: Record<string, string> = {
  new_email: 'blue',
  parsed: 'cyan',
  need_customer_info: 'gold',
  auto_replied: 'purple',
  manual_review: 'orange',
  ready_for_export: 'green',
  error: 'red',
  closed: 'default',
};

export const parseStatusLabels: Record<string, string> = {
  pending: '待解析',
  parsed: '已解析',
  skipped: '已跳过',
  failed: '失败',
};

export const reviewStatusLabels: Record<string, string> = {
  pending: '待审核',
  approved: '已通过',
  rejected: '已驳回',
};

export const taskStatusLabels: Record<string, string> = {
  pending: '待领取',
  assigned: '已分配',
  claimed: '处理中',
  resolved: '已解决',
  closed: '已关闭',
};

export const taskPriorityColors: Record<string, string> = {
  low: 'default',
  normal: 'blue',
  high: 'orange',
  urgent: 'red',
};
