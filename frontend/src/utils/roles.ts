import type { RoleCode } from '../types/api';

export const roleLabels: Record<RoleCode, string> = {
  admin: '系统管理员',
  supervisor: '主管',
  operator: '一般操作员',
};

export const roleDescriptions: Record<RoleCode, string> = {
  admin: '用户管理、角色分配、系统配置、基础资料维护、权限配置、全部数据查看和全部业务兜底操作',
  supervisor: '查看全部业务数据，任务分配、转派、释放，审核回复，处理异常任务',
  operator: '处理本人可见或已领取任务，修正字段、SN 校验、采纳解析、生成追问和提交回复草稿',
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
