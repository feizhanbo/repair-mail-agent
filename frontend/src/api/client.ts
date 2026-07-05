import axios, { type AxiosRequestConfig } from 'axios';
import { useAuthStore } from '../stores/authStore';
import type {
  AiLog,
  ApiResponse,
  BoardCard,
  CurrentUser,
  DashboardSummary,
  EmailDetail,
  EmailIngestRequest,
  EmailItem,
  LoginRequest,
  LoginResponse,
  ManualTask,
  ManualTaskDetail,
  ManualTaskReparseResponse,
  NotificationEvent,
  PageData,
  ReplyRecord,
  SnAsset,
  SystemInfo,
  Ticket,
  TicketDetail,
  UserAccount,
  UserCreateRequest,
  UserUpdateRequest,
} from '../types/api';

export const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? '/api/v1',
  timeout: 15000,
});

export function apiErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const payload = error.response?.data as { message?: string; detail?: string } | undefined;
    if (payload?.message) return payload.message;
    if (payload?.detail) return payload.detail;
    if (error.code === 'ECONNABORTED') return '请求超时，请稍后重试';
    if (!error.response) return '网络连接失败，请检查后端服务';
  }
  return '操作失败，请稍后重试';
}

apiClient.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
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
  ingestEmail: (body: EmailIngestRequest) => postData('/emails/ingest', body),
  reparseEmail: (id: number, body = { mode: 'field_extract' as const }) => postData(`/emails/${id}/reparse`, body),
  tickets: (params: Record<string, unknown>) => getData<PageData<Ticket>>('/tickets', { params }),
  ticketDetail: (id: number) => getData<TicketDetail>(`/tickets/${id}`),
  patchTicketFields: (id: number, body: Record<string, unknown>) => patchData<TicketDetail>(`/tickets/${id}/fields`, body),
  patchTicketItems: (id: number, body: Record<string, unknown>) => patchData<TicketDetail>(`/tickets/${id}/items`, body),
  transitionTicket: (id: number, body: Record<string, unknown>) => postData<TicketDetail>(`/tickets/${id}/transition`, body),
  validateTicketSn: (id: number) => postData<TicketDetail>(`/tickets/${id}/validate-sn`),
  applyParseResult: (id: number, body?: string | { reason?: string; action?: 'apply' | 'partial_apply' | 'reject' }) => {
    const payload = typeof body === 'string' ? { reason: body } : body;
    return postData<TicketDetail>(`/parse-results/${id}/apply`, payload ?? { action: 'apply' });
  },
  manualTasks: (params: Record<string, unknown>) => getData<PageData<ManualTask>>('/manual-review/tasks', { params }),
  manualTaskDetail: (id: number) => getData<ManualTaskDetail>(`/manual-review/tasks/${id}`),
  claimTask: (id: number) => postData<ManualTask>(`/manual-review/tasks/${id}/claim`),
  releaseTask: (id: number) => postData<ManualTask>(`/manual-review/tasks/${id}/release`),
  assignTask: (id: number, body: { assigned_user_id?: number | null; reason?: string }) => postData<ManualTask>(`/manual-review/tasks/${id}/assign`, body),
  resolveTask: (id: number, body: Record<string, unknown>) => postData(`/manual-review/tasks/${id}/resolve`, body),
  reparseTask: (id: number, body: Record<string, unknown>) => postData<ManualTaskReparseResponse>(`/manual-review/tasks/${id}/reparse`, body),
  replies: (params: Record<string, unknown>) => getData<PageData<ReplyRecord>>('/replies', { params }),
  draftReply: (ticketId: number, body: Record<string, unknown>) => postData<ReplyRecord>(`/replies/${ticketId}/draft`, body),
  approveReply: (id: number) => postData(`/replies/${id}/approve-send`),
  rejectReply: (id: number, reason: string) => postData(`/replies/${id}/reject`, { reason }),
  snAssets: (params: Record<string, unknown>) => getData<PageData<SnAsset>>('/master-data/sn-assets', { params }),
  importSnAssets: (body: Record<string, unknown>) => postData('/master-data/sn-assets/import', body),
  boardCards: (params: Record<string, unknown>) => getData<PageData<BoardCard>>('/master-data/board-cards', { params }),
  importBoardCards: (body: Record<string, unknown>) => postData('/master-data/board-cards/import', body),
  aiLogs: (params: Record<string, unknown>) => getData<PageData<AiLog>>('/ai-logs', { params }),
  notifications: (params: Record<string, unknown>) => getData<PageData<NotificationEvent>>('/notifications', { params }),
  markNotificationRead: (id: number) => postData<NotificationEvent>(`/notifications/${id}/read`),
  systemInfo: () => getData<SystemInfo>('/system/info'),
};
