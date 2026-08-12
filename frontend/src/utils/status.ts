export const ticketStatusLabels: Record<string, string> = {
  new_email: '邮件已入库',
  parsed: '已解析',
  need_customer_info: '待客户补充',
  auto_replied: '已追问',
  manual_review: '人工复核',
  ready_for_export: '可导出',
  rma_sent: 'RMA 已发送',
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
  rma_sent: 'cyan',
  error: 'red',
  closed: 'default',
};

export const parseStatusLabels: Record<string, string> = {
  pending: '待解析',
  parsed: '已解析',
  skipped: '已跳过',
  failed: '失败',
  auto_applied: '自动应用',
  manually_applied: '人工应用',
  partially_applied: '部分应用',
  rejected: '已拒绝',
  engineering_reference_stored: '工程辅助资料已保存',
};

export const reviewStatusLabels: Record<string, string> = {
  pending: '待审核',
  approved: '已通过',
  auto_approved: '自动通过',
  rejected: '已驳回',
};

export const taskStatusLabels: Record<string, string> = {
  pending: '待分配',
  assigned: '已分配待处理',
  claimed: '处理中',
  resolved: '已解决',
  closed: '已关闭',
};

export const routeStatusLabels: Record<string, string> = {
  pending: '待匹配',
  resolved: '已匹配',
  needs_manual: '需人工确认',
};

export const sapStatusLabels: Record<string, string> = {
  pending: '待提交 SAP',
  submitting: '提交 SAP 中',
  submitted: '已提交 SAP',
  accepted: 'SAP 已受理',
  waiting_sap_result: '等待 SAP 结果',
  waiting_rma: '等待 RMA 回填',
  submit_unknown: '提交结果未知',
  submit_failed: 'SAP 提交失败',
  awaiting_approval: '待管理员确认',
  applying: '快照应用中',
  succeeded: '快照已生效',
  rma_received: 'RMA 已回填',
  failed: 'SAP 处理失败',
  timed_out: 'SAP 回填超时',
};

export const rmaStatusLabels: Record<string, string> = {
  pending: '待生成 RMA',
  not_required: '无需 RMA',
  generated: 'RMA 已生成',
  sent: 'RMA 已发送',
  failed: 'RMA 处理失败',
  manual_review: 'RMA 需人工复核',
};

export const validationStatusLabels: Record<string, string> = {
  pending: '待校验',
  pass: '校验通过',
  passed: '校验通过',
  failed: '校验失败',
  stale: '需重新校验',
};

export const taskPriorityColors: Record<string, string> = {
  low: 'default',
  normal: 'blue',
  high: 'orange',
  urgent: 'red',
};

export const taskStatusColors: Record<string, string> = {
  pending: 'blue',
  assigned: 'cyan',
  claimed: 'processing',
  resolved: 'green',
  closed: 'default',
};

export const sendStatusLabels: Record<string, string> = {
  pending: '待发送',
  sending: '发送中',
  auto_sending: '自动发送中',
  sent: '已发送',
  send_failed: '发送失败',
  send_uncertain: '发送结果不确定',
  send_disabled: '未启用',
};
