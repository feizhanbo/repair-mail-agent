import { Alert, Collapse, Descriptions, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { DeletePreview } from '../types/api';
import JsonBlock from './JsonBlock';

const LABELS: Record<string, string> = {
  emails: '邮件', email: '邮件', attachments: '附件', email_attachments: '附件',
  parse_results: '解析结果', tickets: '工单', repair_tickets: '工单',
  email_ticket_links: '邮件工单关联', replies: '回复记录', reply_records: '回复记录',
  manual_review_tasks: '人工复核任务', operation_logs: '审计记录', oss_objects: '存储文件',
  auto_send_enabled: '普通回复自动发送', auto_followup_enabled: '自动追问', rma_auto_send_enabled: 'RMA 自动发送',
  auto_apply_min_confidence: '自动应用最低置信度', auto_send_min_confidence: '自动发送最低置信度',
  confidence_threshold: '人工复核置信度阈值', max_follow_up: '最大追问次数',
  imap_fetch_enabled: '自动收取邮件', imap_poll_interval_minutes: '轮询周期（分钟）',
  imap_folder: '收件文件夹', imap_fetch_limit: '单次收取上限', imap_unseen_only: '仅收取未读邮件',
  imap_max_retries: '失败重试次数', imap_archive_to_oss: '归档原始邮件',
  relay_sqlserver_enabled: '启用 SQL Server 中转库', relay_sn_sync_enabled: '启用 SN 同步',
  sn_schema: '来源 Schema', sn_table: '来源表', sn_primary_key: 'SN 主键列',
  sn_updated_at_column: '更新时间列', batch_size: '同步批量大小', snapshot_max_age_hours: '快照最长有效时间（小时）',
};

const BLOCKER_LABELS: Record<string, string> = {
  HAS_LINKED_TICKETS: '存在关联工单，请先解除业务关联',
  HAS_LINKED_EMAILS: '存在关联邮件，请先解除业务关联',
  HAS_ACTIVE_TICKETS: '仍有未完成工单使用该资料',
  HAS_DEPENDENCIES: '存在关联业务数据，暂时无法删除',
  NOT_DELETABLE: '该数据当前不允许删除',
};

const friendlyLabel = (key: string) => LABELS[key] ?? `扩展项目：${key.split('_').join(' ')}`;

const displayValue = (value: unknown): string => {
  if (value === true) return '开启';
  if (value === false) return '关闭';
  if (value === null || value === undefined || value === '') return '未设置';
  if (Array.isArray(value)) return value.length ? value.map(displayValue).join('、') : '无';
  if (typeof value === 'object') return `${Object.keys(value as object).length} 项配置`;
  return String(value);
};

export function ChangePreview({ before, after }: { before?: Record<string, unknown> | null; after: Record<string, unknown> }) {
  const rows = Object.entries(after)
    .filter(([key]) => key !== 'reason')
    .map(([key, value]) => ({ key, label: friendlyLabel(key), before: before?.[key], after: value }))
    .filter((row) => JSON.stringify(row.before) !== JSON.stringify(row.after));
  const columns: ColumnsType<(typeof rows)[number]> = [
    { title: '配置项', dataIndex: 'label', width: 210 },
    { title: '变更前', dataIndex: 'before', render: displayValue },
    { title: '变更后', dataIndex: 'after', render: (value) => <Typography.Text strong>{displayValue(value)}</Typography.Text> },
  ];
  return rows.length ? <Table rowKey="key" size="small" pagination={false} columns={columns} dataSource={rows} /> : <Alert type="info" showIcon message="没有检测到配置变化" />;
}

const blockerText = (value: DeletePreview['blockers'][number]) => {
  if (typeof value === 'string') return BLOCKER_LABELS[value] ?? '存在关联业务数据，暂时无法删除；可展开技术详情查看原因';
  return value.message || BLOCKER_LABELS[value.code ?? ''] || '存在关联业务数据，暂时无法删除；可展开技术详情查看原因';
};

export function DeletionImpactPreview({ preview }: { preview: DeletePreview }) {
  const impactRows = Object.entries(preview.affected_counts ?? {}).map(([key, count]) => ({ key, name: friendlyLabel(key), count }));
  return (
    <div className="drawer-stack">
      <Alert
        type={preview.deletable ? 'warning' : 'error'}
        showIcon
        message={preview.deletable ? '确认后将删除以下业务数据' : '当前数据不能删除'}
        description={preview.deletable ? '该操作会写入审计记录，且部分数据无法恢复。' : preview.blockers.map(blockerText).join('；')}
      />
      <Table rowKey="key" size="small" pagination={false} dataSource={impactRows} columns={[
        { title: '受影响内容', dataIndex: 'name' },
        { title: '数量', dataIndex: 'count', width: 100, render: (value) => <Tag color={Number(value) ? 'orange' : 'default'}>{value}</Tag> },
      ]} />
      <Descriptions bordered size="small" column={1}>
        <Descriptions.Item label="存储文件">{preview.oss_objects?.length ?? 0} 个；共享文件会保留，独占文件按预览结果清理</Descriptions.Item>
        <Descriptions.Item label="不可逆影响">{preview.irreversible_effects?.length ? preview.irreversible_effects.map(displayValue).join('；') : '无额外不可逆影响'}</Descriptions.Item>
      </Descriptions>
      <Collapse ghost items={[{ key: 'technical', label: '技术详情', children: <JsonBlock value={preview} /> }]} />
    </div>
  );
}
