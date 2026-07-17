import type { RoleCode } from '../types/api';

export const roleLabels: Record<RoleCode, string> = {
  admin: '系统管理员',
  supervisor: '主管',
  operator: '一般操作员',
};

export const roleDescriptions: Record<RoleCode, string> = {
  admin: '用户管理、角色分配、系统配置、基础资料维护、权限配置、全部数据查看和全部业务兜底操作',
  supervisor: '保留角色；当前不参与常规任务分配流程',
  operator: '查看并处理全部业务任务、修正字段、执行安全校验、审批发送回复、查看 AI 日志、统计和 SN 主数据',
};

export const roleOptions = (Object.keys(roleLabels) as RoleCode[]).map((value) => ({
  value,
  label: roleLabels[value],
}));

export function hasRole(roles: readonly string[] | undefined, role: RoleCode) {
  return Boolean(roles?.includes(role));
}

export function hasAnyRole(roles: readonly string[] | undefined, allowedRoles: readonly RoleCode[]) {
  return Boolean(roles?.some((role) => allowedRoles.includes(role as RoleCode)));
}
