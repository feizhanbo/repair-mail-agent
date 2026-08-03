import axios, { type AxiosRequestConfig } from 'axios';
import { useAuthStore } from '../stores/authStore';
import type {
  AiLog,
  ApiResponse,
  AsyncIngestResult,
  BoardCard,
  CustomerServicePolicy,
  CurrentUser,
  ContentPreview,
  DatabaseRowsResponse,
  DatabaseTablesResponse,
  DashboardSummary,
  EmailDetail,
  EmailFlowTrace,
  EmailIngestResult,
  EmailIngestRequest,
  EmailItem,
  LoginRequest,
  LoginResponse,
  JobRunLog,
  ImapFetchJobResponse,
  ImapPreflightResult,
  ImapFetchStatus,
  ManualTask,
  ManualTaskDetail,
  ManualTaskReparseResponse,
  MailTestPreflightResult,
  NotificationEvent,
  NotificationCenterPage,
  NotificationCenterSummary,
  ObjectDownloadUrl,
  PageData,
  ReplyTemplate,
  ReplyRecord,
  SnAsset,
  StatisticsSummary,
  SystemConfig,
  SystemInfo,
  SystemRuntimeStatus,
  Ticket,
  TicketDetail,
  UserAccount,
  UserCreateRequest,
  UserUpdateRequest,
} from '../types/api';

export const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? '/api/v1',
  timeout: 15000,
  withCredentials: true,
});

function friendlyServerMessage(status?: number, code?: string): string {
  const messages: Record<string, string> = {
    AUTH_FORBIDDEN: '当前账号没有权限执行此操作',
    REQUEST_VALIDATION_ERROR: '请检查输入内容是否完整、格式是否正确',
    INTERNAL_SERVER_ERROR: '系统暂时无法处理请求，请稍后再试',
    USER_CANNOT_DELETE_SELF: '不能删除当前登录账号',
    USER_HAS_REFERENCES: '该用户仍有关联业务数据，不能删除',
    USER_USERNAME_EXISTS: '账号已存在，请更换账号',
    USER_EMAIL_EXISTS: '邮箱已被使用，请更换邮箱',
    USER_OLD_PASSWORD_INVALID: '原密码不正确',
    USER_PASSWORD_REQUIRED: '请输入新密码',
    USER_PASSWORD_TOO_LONG: '密码过长，请使用 72 字节以内的密码',
    TICKET_NOT_FOUND: '工单不存在或已被删除',
    TICKET_VERSION_CONFLICT: '工单已被其他人更新，请刷新后重试',
    WORKFLOW_TRANSITION_NOT_ALLOWED: '当前状态不支持此操作',
    WORKFLOW_STATUS_NOT_FOUND: '目标状态不可用，请联系管理员',
    TICKET_ALREADY_CLOSED: '工单已关闭，不能继续操作',
    EMAIL_NOT_FOUND: '邮件不存在或已被删除',
    EML_FILE_REQUIRED: '请选择 .eml 格式的邮件文件',
    EML_FILE_EMPTY: '邮件文件为空，请重新选择',
    EML_PARSE_FAILED: '邮件文件格式无法解析',
    EML_FROM_REQUIRED: '邮件中缺少有效发件人地址',
    EMAIL_ARCHIVE_TOO_LARGE: '邮件原件或附件总大小超过归档限制',
    TOO_MANY_ATTACHMENTS: '邮件附件数量超过系统限制',
    OSS_NOT_CONFIGURED: 'OSS 尚未正确配置，邮件未入库',
    OSS_OBJECT_NOT_FOUND: 'OSS 中未找到对应文件，可能已被移除',
    OSS_OBJECT_NOT_READY: '附件仍在归档处理中，请稍后重试',
    OSS_ACCESS_DENIED: 'OSS 拒绝访问该对象，请管理员检查权限',
    OSS_NETWORK_UNREACHABLE: '暂时无法连接 OSS，请稍后重试',
    OSS_DOWNLOAD_FAILED: '附件从 OSS 下载失败，请稍后重试',
    ATTACHMENT_PDF_INVALID: 'PDF 文件已损坏、加密或无法渲染',
    ATTACHMENT_PREVIEW_UNSUPPORTED: '该附件格式仅支持下载',
    ENGINEERING_REFERENCE_NOT_REQUIRED: '工程辅助资料无需参与RMA字段解析',
    TYPE_DECLARATION_MISMATCH: '附件声明类型与实际识别类型不一致',
    ARCHIVE_CONTENT_NOT_SCANNED: '工程辅助压缩附件未经内容扫描',
    AI_LOG_DETAIL_EXPIRED: 'AI 完整日志已过保留期，仅可查看元数据',
    AI_LOG_DETAIL_HASH_MISMATCH: 'AI 日志完整性校验失败，请联系管理员',
    NOTIFICATION_NOT_FOUND: '该通知不属于当前用户或已不存在',
    OSS_ARCHIVAL_FAILED: '邮件归档到 OSS 失败，请稍后重试',
    JOB_POLL_TIMEOUT: '后台任务处理超时，请在邮件详情中查看最新状态',
    IMAP_NOT_CONFIGURED: 'IMAP 账号配置不完整',
    IMAP_CONNECTION_FAILED: 'IMAP TLS 连接或登录失败，请检查配置',
    IMAP_SELECT_FAILED: '无法以只读方式打开指定邮箱文件夹',
    IMAP_UIDVALIDITY_MISSING: '邮箱服务器未返回 UIDVALIDITY，已停止捞取以避免重复邮件',
    ATTACHMENT_CONTENT_INVALID: '附件内容无法读取，请重新选择文件',
    MANUAL_TASK_NOT_FOUND: '复核任务不存在或已被处理',
    TASK_ASSIGNMENT_DISABLED: '当前采用系统自动负责人，不再支持领取、释放或人工分配',
    MANUAL_TASK_ALREADY_RESOLVED: '任务已完成，请刷新列表',
    MANUAL_TASK_NEXT_ACTION_INVALID: '请选择有效的后续动作',
    REPLY_NOT_FOUND: '回复记录不存在或已被删除',
    REPLY_ALREADY_APPROVED: '回复已审核通过，不能再修改',
    REPLY_TEMPLATE_NOT_FOUND: '未找到可用回复话术，请联系管理员配置',
    FOLLOWUP_LIMIT_EXCEEDED: '追问次数已达到上限，请转人工处理',
    REPLY_TEMPLATE_ALREADY_EXISTS: '话术编码和版本已存在，请更换编码或版本',
    REPLY_TEMPLATE_IN_USE: '该话术已有回复记录使用，请停用而不是删除',
    EXPORT_SELECTION_REQUIRED: '请先选择要导出的数据',
    EXPORT_SELECTION_EMPTY: '未找到可导出的已选数据',
    CSV_HEADER_REQUIRED: '导入文件缺少表头，请下载模板后重新填写',
    CSV_VALIDATION_FAILED: '导入文件内容有误，请检查模板字段和日期格式',
    XLSX_HEADER_REQUIRED: '导入文件缺少表头，请下载模板后重新填写',
    XLSX_VALIDATION_FAILED: '导入文件内容有误，请检查模板字段和日期格式',
    XLSX_INVALID_FILE: '导入文件无法读取，请上传 .xlsx 格式文件',
  };
  if (code && messages[code]) return messages[code];
  if (code?.startsWith('ROLE_NOT_ALLOWED')) return '选择的角色不可用';
  if (code?.startsWith('ROLE_NOT_INITIALIZED')) return '角色配置未初始化，请联系管理员';
  if (code?.startsWith('TICKET_FIELD_NOT_ALLOWED')) return '该工单字段暂不支持修改';
  if (code?.startsWith('TICKET_ITEM_FIELD_NOT_ALLOWED')) return '该工单明细字段暂不支持修改';
  if (code?.startsWith('REPLY_FIELD_NOT_ALLOWED')) return '该回复字段暂不支持修改';
  if (status === 400) return '提交内容不符合当前业务规则，请检查后重试';
  if (status === 401) return '登录状态已失效，请重新登录';
  if (status === 403) return '当前账号没有权限执行此操作';
  if (status === 404) return '数据不存在或已被删除';
  if (status === 409) return '数据状态已变化，请刷新后重试';
  if (status === 413) return '上传文件超过代理或系统允许的大小';
  if (status === 422) return '请检查输入内容是否完整、格式是否正确';
  if (status && status >= 500) return '系统暂时无法处理请求，请稍后再试';
  return '操作失败，请稍后重试';
}

export function apiErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    if (error.code === 'ECONNABORTED') return '请求超时，请稍后重试';
    if (!error.response) return '网络连接失败，请检查后端服务';
    const payload = error.response.data as {
      message?: string;
      detail?: string | { code?: string; reasons?: string[]; preflight?: { reasons?: string[] } };
      data?: {
        correlation_id?: string;
        stage?: string;
        preflight?: { reasons?: string[] };
      };
    } | undefined;
    const detail = payload?.detail;
    const code = typeof payload?.message === 'string'
      ? payload.message
      : typeof detail === 'string'
        ? detail
        : detail?.code;
    const preflightReasons = payload?.data?.preflight?.reasons
      ?? (typeof detail === 'object' ? (detail.preflight?.reasons ?? detail.reasons ?? []) : []);
    if (preflightReasons.length) return `邮件预检未通过：${preflightReasons.join('、')}`;
    const base = friendlyServerMessage(error.response.status, code);
    return payload?.data?.correlation_id ? `${base}（关联 ID：${payload.data.correlation_id}）` : base;
  }
  return friendlyServerMessage();
}

apiClient.interceptors.request.use((config) => {
  if (!config.headers['X-Correlation-ID']) {
    config.headers['X-Correlation-ID'] = crypto.randomUUID();
  }
  return config;
});

apiClient.interceptors.response.use(
  (response) => response.data,
  (error) => {
    if (error.response?.status === 401) {
      useAuthStore.getState().clearSession();
    }
    return Promise.reject(error);
  },
);

async function getData<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  const response = await apiClient.get<ApiResponse<T>, ApiResponse<T>>(url, config);
  return response.data;
}

async function postData<T, B = unknown>(url: string, body?: B, config?: AxiosRequestConfig): Promise<T> {
  const response = await apiClient.post<ApiResponse<T>, ApiResponse<T>, B>(url, body as B, config);
  return response.data;
}

async function patchData<T, B = unknown>(url: string, body?: B): Promise<T> {
  const response = await apiClient.patch<ApiResponse<T>, ApiResponse<T>, B>(url, body as B);
  return response.data;
}

export const api = {
  login: (body: LoginRequest) => postData<LoginResponse, LoginRequest>('/auth/login', body),
  logout: () => postData<Record<string, never>, Record<string, never>>('/auth/logout', {}),
  me: () => getData<{ user: Omit<CurrentUser, 'roles'>; roles: string[] }>('/auth/me'),
  updateProfile: (body: UserUpdateRequest) => patchData<UserAccount>('/auth/me/profile', body),
  changePassword: (body: { old_password: string; new_password: string }) => patchData<UserAccount>('/auth/me/password', body),
  users: (params: Record<string, unknown>) => getData<PageData<UserAccount>>('/users', { params }),
  createUser: (body: UserCreateRequest) => postData<UserAccount>('/users', body),
  updateUser: (id: number, body: UserUpdateRequest) => patchData<UserAccount>(`/users/${id}`, body),
  updateUserStatus: (id: number, status: 'active' | 'disabled') => patchData<UserAccount>(`/users/${id}/status`, { status }),
  updateUserRoles: (id: number, roles: string[]) => apiClient.put<ApiResponse<UserAccount>, ApiResponse<UserAccount>>(`/users/${id}/roles`, { roles }).then((response) => response.data),
  resetUserPassword: (id: number, password: string) => postData<UserAccount>(`/users/${id}/reset-password`, { password }),
  deleteUser: (id: number) => apiClient.delete<ApiResponse<{ deleted: boolean; user: UserAccount }>, ApiResponse<{ deleted: boolean; user: UserAccount }>>(`/users/${id}`).then((response) => response.data),
  dashboard: () => getData<DashboardSummary>('/dashboard/summary'),
  emails: (params: Record<string, unknown>) => getData<PageData<EmailItem>>('/emails', { params }),
  emailDetail: (id: number) => getData<EmailDetail>(`/emails/${id}`),
  emailFlowTrace: (id: number) => getData<EmailFlowTrace>(`/emails/${id}/flow-trace`),
  rawEmlDownloadUrl: (id: number) => getData<ObjectDownloadUrl>(`/emails/${id}/raw-eml-url`),
  attachmentDownloadUrl: (id: number) => getData<ObjectDownloadUrl>(`/emails/attachments/${id}/download-url`),
  emailPreview: (id: number) => getData<ContentPreview>(`/emails/${id}/preview`),
  attachmentPreview: (id: number) => getData<ContentPreview>(`/emails/attachments/${id}/preview`),
  exportEmails: (params: Record<string, unknown>) => apiClient.get<Blob, Blob>('/emails/export', { params, responseType: 'blob' }),
  ingestEmail: (body: EmailIngestRequest) => postData<EmailIngestResult, EmailIngestRequest>('/emails/ingest', body),
  ingestEmailJob: (body: EmailIngestRequest) => postData<AsyncIngestResult, EmailIngestRequest>('/emails/ingest/jobs', body),
  ingestEmlFile: (file: File, options?: { mailbox_account?: string; folder_name?: string; auto_parse?: boolean }) => {
    const body = new FormData();
    body.append('file', file);
    body.append('mailbox_account', options?.mailbox_account ?? 'manual-eml');
    body.append('folder_name', options?.folder_name ?? 'INBOX');
    body.append('auto_parse', String(options?.auto_parse ?? true));
    return postData<EmailIngestResult>('/emails/ingest-eml', body, { timeout: 600_000 });
  },
  ingestEmlFileJob: (file: File, options?: { mailbox_account?: string; folder_name?: string }) => {
    const body = new FormData();
    body.append('file', file);
    body.append('mailbox_account', options?.mailbox_account ?? 'manual-eml');
    body.append('folder_name', options?.folder_name ?? 'INBOX');
    return postData<AsyncIngestResult>('/emails/ingest-eml/jobs', body, { timeout: 600_000 });
  },
  reparseEmail: (id: number, body: Record<string, unknown> = {}) => postData(`/emails/${id}/reparse`, body),
  reparseEmailJob: (id: number, body: Record<string, unknown> = {}) => postData<JobRunLog>(`/emails/${id}/reparse/jobs`, body),
  fetchEmailStatus: () => getData<ImapFetchStatus>('/emails/fetch-status'),
  preflightImap: () => postData<ImapPreflightResult>('/emails/fetch/preflight'),
  fetchEmailJob: (params: Record<string, unknown> = {}) => postData<ImapFetchJobResponse>('/emails/fetch/jobs', undefined, { params }),
  tickets: (params: Record<string, unknown>) => getData<PageData<Ticket>>('/tickets', { params }),
  exportTickets: (params: Record<string, unknown>) => apiClient.get<Blob, Blob>('/tickets/export', { params, responseType: 'blob' }),
  exportSelectedTickets: (ids: number[]) => apiClient.post<Blob, Blob>('/tickets/export-selected', { ids }, { responseType: 'blob' }),
  ticketDetail: (id: number) => getData<TicketDetail>(`/tickets/${id}`),
  patchTicketFields: (id: number, body: Record<string, unknown>) => patchData<TicketDetail>(`/tickets/${id}/fields`, body),
  patchTicketItems: (id: number, body: Record<string, unknown>) => patchData<TicketDetail>(`/tickets/${id}/items`, body),
  transitionTicket: (id: number, body: Record<string, unknown>) => postData<TicketDetail>(`/tickets/${id}/transition`, body),
  validateTicketSn: (id: number) => postData<TicketDetail>(`/tickets/${id}/validate-sn`),
  validateTicketExport: (id: number) => postData(`/tickets/${id}/validate-export`),
  retrySapExport: (id: number) => postData<JobRunLog>(`/tickets/${id}/sap-export/retry`),
  pollSapExport: (id: number) => postData<Record<string, unknown>>(`/tickets/${id}/sap-export/poll`),
  confirmLateSapResult: (id: number) => postData<Record<string, unknown>>(`/tickets/${id}/sap-export/confirm-late`),
  reconcileSapSubmission: (
    ticketId: number,
    lineId: number,
    body: { outcome: 'accepted' | 'not_inserted'; reason: string; call_id?: string },
  ) => postData<Record<string, unknown>>(`/tickets/${ticketId}/sap-export/lines/${lineId}/reconcile`, body),
  resolveTicketPolicy: (id: number) =>
    postData<Record<string, unknown>>(`/tickets/${id}/policy/resolve`),
  overrideTicketPolicy: (
    id: number,
    body: {
      charge_status: 'free' | 'annual_contract' | 'chargeable' | 'manual_confirmation';
      customer_scope?: 'domestic' | 'overseas';
      reason: string;
    },
  ) => postData<Record<string, unknown>>(`/tickets/${id}/policy/manual-override`, body),
  resolveReturnRoutes: (id: number) =>
    postData<Record<string, unknown>>(`/tickets/${id}/return-routes/resolve`),
  selectReturnRoute: (
    ticketId: number,
    itemId: number,
    body: { return_location: 'beijing' | 'tianjin'; reason: string },
  ) => postData<Record<string, unknown>>(
    `/tickets/${ticketId}/items/${itemId}/return-route/manual`,
    body,
  ),
  retryRmaSend: (id: number) => postData<JobRunLog>(`/tickets/${id}/rma/retry-send`),
  approveRmaManualPolicy: (
    id: number,
    body: {
      reason: string;
      confirm_policy_values: true;
      confirm_template_thread_and_archive: true;
    },
  ) => postData<JobRunLog>(`/tickets/${id}/rma/manual-policy-approve`, body),
  confirmDeviceReceived: (id: number, body: { idempotency_key: string; note?: string }) =>
    postData<{ ticket_id: number; status: string; reply_id?: number | null; idempotent_reuse: boolean }>(`/tickets/${id}/confirm-device-received`, body),
  applyParseResult: (id: number, body?: string | { reason?: string; action?: 'apply' | 'partial_apply' | 'reject'; selected_fields?: string[]; selected_item_indices?: number[] }) => {
    const payload = typeof body === 'string' ? { reason: body } : body;
    return postData<TicketDetail>(`/parse-results/${id}/apply`, payload ?? { action: 'apply' });
  },
  manualTasks: (params: Record<string, unknown>) => getData<PageData<ManualTask>>('/manual-review/tasks', { params }),
  manualTaskDetail: (id: number) => getData<ManualTaskDetail>(`/manual-review/tasks/${id}`),
  resolveTask: (id: number, body: Record<string, unknown>) => postData(`/manual-review/tasks/${id}/resolve`, body),
  reparseTask: (id: number, body: Record<string, unknown>) => postData<ManualTaskReparseResponse>(`/manual-review/tasks/${id}/reparse`, body),
  replies: (params: Record<string, unknown>) => getData<PageData<ReplyRecord>>('/replies', { params }),
  draftReply: (ticketId: number, body: Record<string, unknown>) => postData<ReplyRecord>(`/replies/${ticketId}/draft`, body),
  approveReply: (id: number) => postData(`/replies/${id}/approve-send`),
  reconcileReplySend: (id: number, body: { outcome: 'sent' | 'failed'; reason: string; smtp_message_id?: string }) =>
    postData<ReplyRecord>(`/replies/${id}/reconcile-send`, body),
  retryReplyArchive: (id: number) => postData<Record<string, unknown>>(`/replies/${id}/retry-archive`),
  approveReplyJob: (id: number) => postData<{ reply: ReplyRecord; job?: JobRunLog | null }>(`/replies/${id}/approve-send/jobs`),
  rejectReply: (id: number, reason: string) => postData(`/replies/${id}/reject`, { reason }),
  snAssets: (params: Record<string, unknown>) => getData<PageData<SnAsset>>('/master-data/sn-assets', { params }),
  importSnAssets: (body: Record<string, unknown>) => postData('/master-data/sn-assets/import', body),
  snAssetsTemplate: () => apiClient.get<Blob, Blob>('/master-data/sn-assets/template', { responseType: 'blob' }),
  exportSnAssets: (params: Record<string, unknown>) => apiClient.get<Blob, Blob>('/master-data/sn-assets/export', { params, responseType: 'blob' }),
  exportSelectedSnAssets: (ids: number[]) => apiClient.post<Blob, Blob>('/master-data/sn-assets/export-selected', { ids }, { responseType: 'blob' }),
  importSnAssetsFile: (file: File) => {
    const body = new FormData();
    body.append('file', file);
    return postData('/master-data/sn-assets/import-file', body, { headers: { 'Content-Type': 'multipart/form-data' } });
  },
  importSnAssetsFileJob: (file: File) => {
    const body = new FormData();
    body.append('file', file);
    return postData<JobRunLog>('/master-data/sn-assets/import-file/jobs', body, { headers: { 'Content-Type': 'multipart/form-data' } });
  },
  boardCards: (params: Record<string, unknown>) => getData<PageData<BoardCard>>('/master-data/board-cards', { params }),
  customerPolicies: (params: Record<string, unknown>) => getData<PageData<CustomerServicePolicy>>('/master-data/customer-policies', { params }),
  createCustomerPolicy: (body: Record<string, unknown>) => postData<CustomerServicePolicy>('/master-data/customer-policies', body),
  updateCustomerPolicy: (id: number, body: Record<string, unknown>) => patchData<CustomerServicePolicy>(`/master-data/customer-policies/${id}`, body),
  importBoardCards: (body: Record<string, unknown>) => postData('/master-data/board-cards/import', body),
  boardCardsTemplate: () => apiClient.get<Blob, Blob>('/master-data/board-cards/template', { responseType: 'blob' }),
  exportBoardCards: (params: Record<string, unknown>) => apiClient.get<Blob, Blob>('/master-data/board-cards/export', { params, responseType: 'blob' }),
  exportSelectedBoardCards: (ids: number[]) => apiClient.post<Blob, Blob>('/master-data/board-cards/export-selected', { ids }, { responseType: 'blob' }),
  importBoardCardsFile: (file: File) => {
    const body = new FormData();
    body.append('file', file);
    return postData('/master-data/board-cards/import-file', body, { headers: { 'Content-Type': 'multipart/form-data' } });
  },
  importBoardCardsFileJob: (file: File) => {
    const body = new FormData();
    body.append('file', file);
    return postData<JobRunLog>('/master-data/board-cards/import-file/jobs', body, { headers: { 'Content-Type': 'multipart/form-data' } });
  },
  job: (id: number) => getData<JobRunLog>(`/jobs/${id}`),
  jobs: (params: Record<string, unknown>) => getData<PageData<JobRunLog>>('/jobs', { params }),
  jobDownloadUrl: (id: number) => getData<{ job_id: number; object_id: number; url: string; expires_seconds: number }>(`/jobs/${id}/download-url`),
  createExportJob: (body: { kind: string; filters?: Record<string, unknown>; ids?: number[] }) => postData<JobRunLog>('/exports/jobs', body),
  aiLogs: (params: Record<string, unknown>) => getData<PageData<AiLog>>('/ai-logs', { params }),
  aiLogDetail: (id: number) => getData<Record<string, unknown>>(`/ai-logs/${id}/detail`),
  notifications: (params: Record<string, unknown>) => getData<PageData<NotificationEvent>>('/notifications', { params }),
  notificationCenter: (params: Record<string, unknown> = {}) => getData<NotificationCenterPage>('/notifications/center', { params }),
  notificationCenterSummary: () => getData<NotificationCenterSummary>('/notifications/center/summary'),
  markNotificationCenterGroupRead: (ticketId: number) => postData<{ ticket_id: number }>(`/notifications/center/${ticketId}/read`),
  markNotificationRead: (id: number) => postData<NotificationEvent>(`/notifications/${id}/read`),
  systemInfo: () => getData<SystemInfo>('/system/info'),
  systemRuntimeStatus: () => getData<SystemRuntimeStatus>('/system/runtime-status'),
  systemConfig: () => getData<SystemConfig>('/system/config'),
  mailTestPreflight: () => postData<MailTestPreflightResult>('/system/mail-test/preflight'),
  updateSystemConfig: (body: Partial<Pick<SystemConfig, 'auto_send_enabled' | 'auto_followup_enabled' | 'rma_auto_send_enabled' | 'auto_send_min_confidence' | 'confidence_threshold' | 'max_follow_up'>>) =>
    patchData<SystemConfig>('/system/config', body),
  replyTemplates: () => getData<ReplyTemplate[]>('/system/reply-templates'),
  createReplyTemplate: (body: Omit<ReplyTemplate, 'id' | 'created_by_user_id' | 'created_at' | 'updated_at'>) =>
    postData<ReplyTemplate>('/system/reply-templates', body),
  updateReplyTemplate: (id: number, body: Partial<Pick<ReplyTemplate, 'template_name' | 'subject_template' | 'body_template' | 'enabled'>>) =>
    patchData<ReplyTemplate>(`/system/reply-templates/${id}`, body),
  deleteReplyTemplate: (id: number) => apiClient.delete<ApiResponse<{ deleted: boolean; template: ReplyTemplate }>, ApiResponse<{ deleted: boolean; template: ReplyTemplate }>>(`/system/reply-templates/${id}`).then((response) => response.data),
  statistics: (params: Record<string, unknown>) => getData<StatisticsSummary>('/statistics/summary', { params }),
  dbTables: () => getData<DatabaseTablesResponse>('/db-browser/tables'),
  dbRows: (tableName: string, params: { page?: number; page_size?: number }) =>
    getData<DatabaseRowsResponse>(`/db-browser/tables/${encodeURIComponent(tableName)}/rows`, { params }),
};
