import { DeleteOutlined, EditOutlined, PlusOutlined, SyncOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Descriptions, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useEffect, useState } from 'react';
import { api, apiErrorMessage } from '../api/client';
import PageTitle from '../components/PageTitle';
import { ChangePreview } from '../components/FriendlyPreview';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import { useAuthStore } from '../stores/authStore';
import type { ReplyTemplate, SnSyncConfig, WorkflowStatus, WorkflowTransition } from '../types/api';
import { formatTime } from '../utils/format';
import { hasAnyRole } from '../utils/roles';

type ConfigForm = {
  auto_send_enabled: boolean;
  auto_followup_enabled: boolean;
  rma_auto_send_enabled: boolean;
  auto_apply_min_confidence: number;
  auto_send_min_confidence: number;
  confidence_threshold: number;
  max_follow_up: number;
  imap_fetch_enabled: boolean;
  imap_poll_interval_minutes: number;
  imap_folder: string;
  imap_fetch_limit: number;
  imap_unseen_only: boolean;
  imap_max_retries: number;
  imap_archive_to_oss: boolean;
};

type SnConfigForm = Omit<SnSyncConfig, 'connection' | 'sn_column_map'>;
type SnMappingRow = { key: string; localField: string; label: string; sourceColumn: string; required: boolean; example: string };

const SN_FIELDS: Array<Omit<SnMappingRow, 'sourceColumn'>> = [
  { key: 'sn', localField: 'sn', label: 'SN', required: true, example: 'SN00001234' },
  { key: 'customer_code', localField: 'customer_code', label: '客户代码', required: true, example: 'C10001' },
  { key: 'customer_name', localField: 'customer_name', label: '客户名称', required: true, example: '示例客户' },
  { key: 'material_code', localField: 'material_code', label: 'SAP 物料代码', required: true, example: 'MAT-001' },
  { key: 'material_name', localField: 'material_name', label: 'SAP 物料名称', required: false, example: '控制板卡' },
  { key: 'asset_status', localField: 'asset_status', label: '资产状态', required: false, example: 'valid' },
  { key: 'service_tracking_card_no', localField: 'service_tracking_card_no', label: '服务追踪卡号', required: false, example: 'STC-001' },
  { key: 'parent_sn', localField: 'parent_sn', label: '上级 SN', required: false, example: 'PARENT-SN' },
  { key: 'top_sn', localField: 'top_sn', label: 'Top SN', required: false, example: 'TOP-SN' },
];

type TemplateForm = {
  template_code: string;
  template_name: string;
  template_type: string;
  language: string;
  version: string;
  subject_template?: string | null;
  body_template: string;
  html_body_template?: string | null;
  enabled: boolean;
};

export default function SystemPage() {
  const canAdmin = hasAnyRole(useAuthStore((state) => state.user?.roles), ['admin']);
  const queryClient = useQueryClient();
  const [configForm] = Form.useForm<ConfigForm>();
  const [templateForm] = Form.useForm<TemplateForm>();
  const [snConfigForm] = Form.useForm<SnConfigForm>();
  const [snMappings, setSnMappings] = useState<SnMappingRow[]>(SN_FIELDS.map((field) => ({ ...field, sourceColumn: '' })));
  const [editingTemplate, setEditingTemplate] = useState<ReplyTemplate | null>(null);
  const [templateModalOpen, setTemplateModalOpen] = useState(false);
  const systemQuery = useQuery({
    queryKey: ['system-info'],
    queryFn: api.systemInfo,
    enabled: canAdmin,
  });
  const configQuery = useQuery({
    queryKey: ['system-config'],
    queryFn: api.systemConfig,
    enabled: canAdmin,
  });
  const runtimeQuery = useQuery({
    queryKey: ['system-runtime-status'],
    queryFn: api.systemRuntimeStatus,
    refetchInterval: 10000,
    enabled: canAdmin,
  });
  const templatesQuery = useQuery({
    queryKey: ['system-reply-templates'],
    queryFn: api.replyTemplates,
    enabled: canAdmin,
  });
  const snConfigQuery = useQuery({ queryKey: ['sn-sync-config'], queryFn: api.snSyncConfig });
  const latestSnSyncQuery = useQuery({ queryKey: ['sn-sync-latest'], queryFn: api.latestSnSync, refetchInterval: 10000 });
  const configMutation = useMutation({
    mutationFn: (values: ConfigForm) => api.updateSystemConfig(values),
    onSuccess: () => {
      message.success('系统配置已保存');
      void queryClient.invalidateQueries({ queryKey: ['system-info'] });
      void queryClient.invalidateQueries({ queryKey: ['system-config'] });
    },
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const snConfigMutation = useMutation({
    mutationFn: (values: Partial<SnSyncConfig>) => api.updateSnSyncConfig(values),
    onSuccess: () => { message.success('SN 同步配置已保存'); void queryClient.invalidateQueries({ queryKey: ['sn-sync-config'] }); },
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const snSyncMutation = useMutation({
    mutationFn: api.startSnSync,
    onSuccess: () => { message.success('SN 同步任务已执行'); void queryClient.invalidateQueries({ queryKey: ['sn-sync-latest'] }); void queryClient.invalidateQueries({ queryKey: ['sn-assets'] }); },
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const mailPreflightMutation = useMutation({
    mutationFn: api.mailTestPreflight,
    onSuccess: (result) => message.success(`邮件预检通过，实际发送 ${result.messages_sent} 封邮件`),
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const templateMutation = useMutation({
    mutationFn: ({ id, values }: { id: number; values: TemplateForm }) =>
      api.updateReplyTemplate(id, {
        template_name: values.template_name,
        subject_template: values.subject_template,
        body_template: values.body_template,
        html_body_template: values.html_body_template,
        enabled: values.enabled,
      }),
    onSuccess: () => {
      message.success('回复话术已保存');
      setEditingTemplate(null);
      setTemplateModalOpen(false);
      templateForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['system-reply-templates'] });
    },
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const templateCreateMutation = useMutation({
    mutationFn: (values: TemplateForm) => api.createReplyTemplate(values),
    onSuccess: () => {
      message.success('回复话术已新增');
      setTemplateModalOpen(false);
      templateForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['system-reply-templates'] });
    },
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const templateDeleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteReplyTemplate(id),
    onSuccess: () => {
      message.success('回复话术已删除');
      void queryClient.invalidateQueries({ queryKey: ['system-reply-templates'] });
    },
    onError: (error) => message.error(apiErrorMessage(error)),
  });

  const info = systemQuery.data;
  const integrations = info?.integrations ?? {};
  useEffect(() => {
    if (configQuery.data) {
      configForm.setFieldsValue({
        auto_send_enabled: configQuery.data.auto_send_enabled,
        auto_followup_enabled: configQuery.data.auto_followup_enabled,
        rma_auto_send_enabled: configQuery.data.rma_auto_send_enabled,
        auto_apply_min_confidence: configQuery.data.auto_apply_min_confidence,
        auto_send_min_confidence: configQuery.data.auto_send_min_confidence,
        confidence_threshold: configQuery.data.confidence_threshold,
        max_follow_up: configQuery.data.max_follow_up,
        imap_fetch_enabled: configQuery.data.imap_fetch_enabled,
        imap_poll_interval_minutes: configQuery.data.imap_poll_interval_minutes,
        imap_folder: configQuery.data.imap_folder,
        imap_fetch_limit: configQuery.data.imap_fetch_limit,
        imap_unseen_only: configQuery.data.imap_unseen_only,
        imap_max_retries: configQuery.data.imap_max_retries,
        imap_archive_to_oss: configQuery.data.imap_archive_to_oss,
      });
    }
  }, [configForm, configQuery.data]);
  useEffect(() => {
    if (!snConfigQuery.data) return;
    snConfigForm.setFieldsValue(snConfigQuery.data);
    setSnMappings(SN_FIELDS.map((field) => ({ ...field, sourceColumn: snConfigQuery.data.sn_column_map[field.localField] ?? '' })));
  }, [snConfigForm, snConfigQuery.data]);

  const statusColumns: ColumnsType<WorkflowStatus> = [
    { title: '状态码', dataIndex: 'status_code', width: 170 },
    { title: '名称', dataIndex: 'status_name', width: 130 },
    { title: '类别', dataIndex: 'status_category', width: 120 },
    { title: '终态', dataIndex: 'is_terminal', width: 90, render: (value: boolean) => <Tag>{value ? '是' : '否'}</Tag> },
    { title: '说明', dataIndex: 'description', ellipsis: true },
  ];
  const transitionColumns: ColumnsType<WorkflowTransition> = [
    { title: '来源', dataIndex: 'from_status_code', width: 140, render: (value: string) => <StatusTag value={value} kind="ticket" /> },
    { title: '目标', dataIndex: 'to_status_code', width: 140, render: (value: string) => <StatusTag value={value} kind="ticket" /> },
    { title: '事件', dataIndex: 'trigger_event', width: 190 },
    { title: '人工', dataIndex: 'require_manual', width: 90, render: (value: boolean) => <Tag>{value ? '是' : '否'}</Tag> },
    { title: '条件', dataIndex: 'condition_desc', ellipsis: true },
  ];
  const templateColumns: ColumnsType<ReplyTemplate> = [
    { title: '编码', dataIndex: 'template_code', width: 150 },
    { title: '名称', dataIndex: 'template_name', width: 180 },
    { title: '类型', dataIndex: 'template_type', width: 120 },
    { title: '语言', dataIndex: 'language', width: 100 },
    { title: '版本', dataIndex: 'version', width: 100 },
    { title: '启用', dataIndex: 'enabled', width: 90, render: (value: boolean) => <Tag color={value ? 'green' : 'default'}>{value ? '启用' : '停用'}</Tag> },
    { title: '更新时间', dataIndex: 'updated_at', width: 160, render: formatTime },
    {
      title: '操作',
      width: 170,
      render: (_, record) => (
        <Space>
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => {
              setEditingTemplate(record);
              templateForm.setFieldsValue({
                template_code: record.template_code,
                template_name: record.template_name,
                template_type: record.template_type,
                language: record.language,
                version: record.version,
                subject_template: record.subject_template ?? undefined,
                body_template: record.body_template,
                html_body_template: record.html_body_template,
                enabled: record.enabled,
              });
              setTemplateModalOpen(true);
            }}
          >
            编辑
          </Button>
          <Button
            size="small"
            danger
            icon={<DeleteOutlined />}
            onClick={() => Modal.confirm({
              title: '确认删除该回复话术？',
              content: '已被回复记录使用的话术不能删除，请改为停用。',
              okText: '删除',
              okButtonProps: { danger: true },
              onOk: () => templateDeleteMutation.mutateAsync(record.id),
            })}
          >
            删除
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div className="page-stack">
      <PageTitle title="系统配置" />
      <SectionPanel>
        <div className="section-heading">
          <Typography.Title level={4}>SN 同步与配置</Typography.Title>
          <Button type="primary" icon={<SyncOutlined spin={snSyncMutation.isPending} />} loading={snSyncMutation.isPending} onClick={() => Modal.confirm({
            title: '确认执行 SN 全量同步？',
            content: <Descriptions bordered size="small" column={1}><Descriptions.Item label="数据源">{String(snConfigQuery.data?.connection?.adapter ?? '未配置')}</Descriptions.Item><Descriptions.Item label="来源表">{snConfigQuery.data ? `${snConfigQuery.data.sn_schema}.${snConfigQuery.data.sn_table}` : '未配置'}</Descriptions.Item><Descriptions.Item label="说明">同步过程会校验重复 SN、必填字段和快照完整性，并写入审计记录。</Descriptions.Item></Descriptions>,
            okText: '确认同步',
            onOk: () => snSyncMutation.mutateAsync(),
          })}>立即同步</Button>
        </div>
        {latestSnSyncQuery.data ? <Descriptions size="small" bordered column={4} style={{ marginBottom: 16 }}>
          <Descriptions.Item label="最近批次">{latestSnSyncQuery.data.batch_no}</Descriptions.Item>
          <Descriptions.Item label="状态"><StatusTag value={latestSnSyncQuery.data.status} kind="sap" /></Descriptions.Item>
          <Descriptions.Item label="来源 / 有效">{latestSnSyncQuery.data.source_count} / {latestSnSyncQuery.data.valid_count}</Descriptions.Item>
          <Descriptions.Item label="重复数">{latestSnSyncQuery.data.duplicate_count}</Descriptions.Item>
        </Descriptions> : <Alert type="info" showIcon message="暂无 SN 同步记录" style={{ marginBottom: 16 }} />}
        <Form<SnConfigForm> form={snConfigForm} layout="vertical" onFinish={(values) => {
          const sn_column_map = Object.fromEntries(snMappings.filter((row) => row.sourceColumn.trim()).map((row) => [row.localField, row.sourceColumn.trim()]));
          const next = { ...values, sn_column_map };
          Modal.confirm({ title: '确认修改 SN 同步配置？', width: 760, content: <ChangePreview before={snConfigQuery.data as unknown as Record<string, unknown>} after={{ ...values, sn_column_map: `${Object.keys(sn_column_map).length} 项字段映射` }} />, okText: '确认提交', onOk: () => snConfigMutation.mutateAsync(next) });
        }}>
          <Space wrap align="start">
            <Form.Item label="启用 SQL Server 中转库" name="relay_sqlserver_enabled" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item label="启用 SN 同步" name="relay_sn_sync_enabled" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item label="来源 Schema" name="sn_schema" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item label="来源表" name="sn_table" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item label="SN 主键列" name="sn_primary_key" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item label="更新时间列" name="sn_updated_at_column"><Input /></Form.Item>
            <Form.Item label="同步批量大小" name="batch_size" rules={[{ required: true }]}><InputNumber min={1} max={10000} /></Form.Item>
            <Form.Item label="快照有效期（小时）" name="snapshot_max_age_hours" rules={[{ required: true }]}><InputNumber min={1} max={720} /></Form.Item>
          </Space>
          <Typography.Title level={5}>字段映射</Typography.Title>
          <Table<SnMappingRow> rowKey="key" size="small" pagination={false} dataSource={snMappings} columns={[
            { title: '本地业务字段', dataIndex: 'label', width: 180 },
            { title: '来源数据库列', dataIndex: 'sourceColumn', render: (_, row) => <Input value={row.sourceColumn} placeholder="例如 SERIAL_NO" onChange={(event) => setSnMappings((items) => items.map((item) => item.key === row.key ? { ...item, sourceColumn: event.target.value } : item))} /> },
            { title: '要求', dataIndex: 'required', width: 90, render: (value: boolean) => <Tag color={value ? 'red' : 'default'}>{value ? '必填' : '可选'}</Tag> },
            { title: '示例值', dataIndex: 'example', width: 160 },
          ]} />
          <Button type="primary" htmlType="submit" loading={snConfigMutation.isPending} style={{ marginTop: 16 }}>保存 SN 配置</Button>
        </Form>
      </SectionPanel>
      {canAdmin ? <>
      <SectionPanel>
        <div className="section-heading">
          <Typography.Title level={4}>运行配置</Typography.Title>
        </div>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={configQuery.data?.environment_note ?? '测试环境默认生成草稿并由人工确认后发送；生产环境可切换为自动发送。'}
        />
        {configQuery.data?.mail_test_static_ready === false ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="测试邮箱静态配置未通过，发送开关不能开启"
            description={(configQuery.data.mail_test_static_reasons ?? []).join('、')}
          />
        ) : null}
        <Form<ConfigForm>
          form={configForm}
          layout="inline"
          className="filter-bar"
          onFinish={(values) => Modal.confirm({ title: '确认修改系统配置？', width: 760, content: <ChangePreview before={configQuery.data as unknown as Record<string, unknown>} after={values as unknown as Record<string, unknown>} />, okText: '确认提交', onOk: () => configMutation.mutateAsync(values) })}
        >
          <Form.Item label="普通回复自动发送" name="auto_send_enabled" valuePropName="checked">
            <Switch disabled={configQuery.data?.mail_test_static_ready === false && !configQuery.data?.auto_send_enabled} />
          </Form.Item>
          <Form.Item label="缺失必填字段自动追问" name="auto_followup_enabled" valuePropName="checked">
            <Switch disabled={configQuery.data?.mail_test_static_ready === false && !configQuery.data?.auto_followup_enabled} />
          </Form.Item>
          <Form.Item label="自动附带 RMA 授权单" name="rma_auto_send_enabled" valuePropName="checked">
            <Switch disabled={configQuery.data?.mail_test_static_ready === false && !configQuery.data?.rma_auto_send_enabled} />
          </Form.Item>
          <Form.Item label="置信度阈值" name="confidence_threshold" rules={[{ required: true }]}>
            <InputNumber min={0} max={1} step={0.01} precision={2} />
          </Form.Item>
          <Form.Item label="自动采纳安全阈值" name="auto_apply_min_confidence" rules={[{ required: true }]}>
            <InputNumber min={0} max={1} step={0.01} precision={2} />
          </Form.Item>
          <Form.Item label="自动发送安全阈值" name="auto_send_min_confidence" rules={[{ required: true }]}>
            <InputNumber min={0} max={1} step={0.01} precision={2} />
          </Form.Item>
          <Form.Item label="追问上限" name="max_follow_up" rules={[{ required: true }]}>
            <InputNumber min={0} max={20} precision={0} />
          </Form.Item>
          <Form.Item label="自动收取邮件" name="imap_fetch_enabled" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item label="轮询周期（分钟）" name="imap_poll_interval_minutes" rules={[{ required: true }]}><InputNumber min={1} max={1440} /></Form.Item>
          <Form.Item label="收件文件夹" name="imap_folder" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item label="单次收取上限" name="imap_fetch_limit" rules={[{ required: true }]}><InputNumber min={1} max={1000} /></Form.Item>
          <Form.Item label="仅收取未读邮件" name="imap_unseen_only" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item label="失败重试次数" name="imap_max_retries" rules={[{ required: true }]}><InputNumber min={0} max={20} /></Form.Item>
          <Form.Item label="归档原始邮件" name="imap_archive_to_oss" valuePropName="checked"><Switch /></Form.Item>
          <Button type="primary" htmlType="submit" loading={configMutation.isPending || configQuery.isFetching}>
            保存
          </Button>
          <Button loading={mailPreflightMutation.isPending} onClick={() => mailPreflightMutation.mutate()}>
            执行邮件预检（不发信）
          </Button>
        </Form>
        {mailPreflightMutation.data ? (
          <Alert
            type="success"
            showIcon
            style={{ marginTop: 12 }}
            message="邮件预检通过（未发送邮件）"
            description={`数据库 ${mailPreflightMutation.data.database?.current_revision ?? '-'}；IMAP 只读检查通过；SMTP 阶段 ${mailPreflightMutation.data.smtp?.stage ?? '-'}；实际发送 ${mailPreflightMutation.data.messages_sent} 封。`}
          />
        ) : null}
      </SectionPanel>
      <SectionPanel>
        <div className="section-heading">
          <Typography.Title level={4}>运行状态</Typography.Title>
        </div>
        <Descriptions column={3} size="small" bordered>
          <Descriptions.Item label="失败任务">{runtimeQuery.data?.failed_job_count ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="等待重试任务">{runtimeQuery.data?.retry_job_count ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="IMAP 待重试">{runtimeQuery.data?.imap_retry_count ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="待补齐归档证据工单">{runtimeQuery.data?.rma_sent_pending_closure_count ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="OSS 孤立对象">{runtimeQuery.data?.oss_orphan_count ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="最近 IMAP 状态">{runtimeQuery.data?.latest_imap_job?.status ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="最近 IMAP 失败数">{runtimeQuery.data?.latest_imap_job?.failed_count ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="DeepSeek 最近状态">{runtimeQuery.data?.ai_provider_status.deepseek?.status ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="Qwen 最近状态">{runtimeQuery.data?.ai_provider_status.qwen?.status ?? '-'}</Descriptions.Item>
        </Descriptions>
      </SectionPanel>
      <SectionPanel>
        <div className="section-heading">
          <Typography.Title level={4}>回复话术</Typography.Title>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              setEditingTemplate(null);
              templateForm.resetFields();
              templateForm.setFieldsValue({ language: 'zh-CN', version: '1', enabled: true });
              setTemplateModalOpen(true);
            }}
          >
            新增话术
          </Button>
        </div>
        <Table<ReplyTemplate>
          rowKey="id"
          loading={templatesQuery.isFetching}
          dataSource={templatesQuery.data ?? []}
          columns={templateColumns}
          pagination={{ pageSize: 8 }}
          size="middle"
        />
      </SectionPanel>
      <SectionPanel>
        <Descriptions column={3} size="small" bordered>
          <Descriptions.Item label="应用">{info?.app ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="环境">{info?.env ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="普通回复自动发送">{info?.auto_send_enabled ? '开启' : '关闭'}</Descriptions.Item>
          <Descriptions.Item label="RMA 自动发送">{info?.rma_auto_send_enabled ? '开启' : '关闭'}</Descriptions.Item>
          <Descriptions.Item label="追问上限">{info?.max_follow_up ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="置信度阈值">{info?.confidence_threshold ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="自动采纳安全阈值">{info?.auto_apply_min_confidence ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="自动发送安全阈值">{info?.auto_send_min_confidence ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="文本 AI 状态">
            <Tag color={integrations.text_ai_configured ? 'green' : 'default'}>{integrations.text_ai_configured ? '已配置' : '未配置'}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label="文本 AI Provider">{String(integrations.text_ai_provider ?? '-')}</Descriptions.Item>
          <Descriptions.Item label="文本 AI 模型">{String(integrations.ai_model ?? '-')}</Descriptions.Item>
          <Descriptions.Item label="文本 AI Base URL">{String(integrations.ai_base_url ?? '-')}</Descriptions.Item>
          <Descriptions.Item label="Prompt 版本">{String(integrations.ai_prompt_version ?? '-')}</Descriptions.Item>
          <Descriptions.Item label="AI 超时">{integrations.ai_timeout_seconds ? `${String(integrations.ai_timeout_seconds)}s` : '-'}</Descriptions.Item>
          <Descriptions.Item label="多模态 AI 状态">
            <Tag color={integrations.multimodal_ai_configured ? 'green' : 'default'}>{integrations.multimodal_ai_configured ? '已配置' : '未配置'}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label="多模态 Provider">{String(integrations.multimodal_provider ?? '-')}</Descriptions.Item>
          <Descriptions.Item label="Qwen VL 模型">{String(integrations.qwen_vl_model ?? '-')}</Descriptions.Item>
          <Descriptions.Item label="SN 中转同步">{integrations.relay_sn_sync_enabled ? '开启' : '关闭'}</Descriptions.Item>
          <Descriptions.Item label="解析结果推送">{integrations.relay_push_enabled ? '开启' : '关闭'}</Descriptions.Item>
          <Descriptions.Item label="中转配置">{integrations.relay_configured ? '已配置' : '未配置'}</Descriptions.Item>
        </Descriptions>
      </SectionPanel>
      <SectionPanel>
        <div className="section-heading">
          <Typography.Title level={4}>状态定义</Typography.Title>
        </div>
        <Table<WorkflowStatus>
          rowKey="id"
          loading={systemQuery.isFetching}
          dataSource={info?.workflow_statuses ?? []}
          columns={statusColumns}
          pagination={false}
          size="middle"
        />
      </SectionPanel>
      <SectionPanel>
        <div className="section-heading">
          <Typography.Title level={4}>状态流转</Typography.Title>
        </div>
        <Table<WorkflowTransition>
          rowKey="id"
          loading={systemQuery.isFetching}
          dataSource={info?.workflow_transitions ?? []}
          columns={transitionColumns}
          pagination={{ pageSize: 12 }}
          size="middle"
        />
      </SectionPanel>
      <Modal
        title={editingTemplate ? '编辑回复话术' : '新增回复话术'}
        open={templateModalOpen}
        onCancel={() => {
          setTemplateModalOpen(false);
          setEditingTemplate(null);
        }}
        footer={null}
        destroyOnClose
      >
        {templateModalOpen ? (
          <Form<TemplateForm>
            form={templateForm}
            layout="vertical"
            onFinish={(values) => {
              Modal.confirm({
                title: editingTemplate ? '确认修改回复话术？' : '确认新增回复话术？',
                width: 680,
                content: <ChangePreview before={(editingTemplate ?? {}) as unknown as Record<string, unknown>} after={values as unknown as Record<string, unknown>} />,
                okText: '确认提交',
                onOk: () => editingTemplate
                  ? templateMutation.mutateAsync({ id: editingTemplate.id, values })
                  : templateCreateMutation.mutateAsync(values),
              });
            }}
          >
            <Form.Item label="编码" name="template_code" rules={[{ required: true }]}>
              <Input disabled={Boolean(editingTemplate)} />
            </Form.Item>
            <Form.Item label="名称" name="template_name" rules={[{ required: true }]}>
              <Input />
            </Form.Item>
            <Form.Item label="类型" name="template_type" rules={[{ required: true }]}>
              <Input disabled={Boolean(editingTemplate)} />
            </Form.Item>
            <div className="two-column-grid">
              <Form.Item label="语言" name="language" rules={[{ required: true }]}>
                <Input disabled={Boolean(editingTemplate)} />
              </Form.Item>
              <Form.Item label="版本" name="version" rules={[{ required: true }]}>
                <Input disabled={Boolean(editingTemplate)} />
              </Form.Item>
            </div>
            <Form.Item label="主题模板" name="subject_template">
              <Input />
            </Form.Item>
            <Form.Item label="正文模板" name="body_template" rules={[{ required: true }]}>
              <Input.TextArea rows={10} />
            </Form.Item>
            <Form.Item label="HTML 正文模板" name="html_body_template" extra="可选；留空时系统会由纯文本模板安全生成 HTML。">
              <Input.TextArea rows={10} />
            </Form.Item>
            <Form.Item label="启用" name="enabled" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={templateMutation.isPending || templateCreateMutation.isPending}>保存</Button>
              <Button onClick={() => {
                setTemplateModalOpen(false);
                setEditingTemplate(null);
              }}>取消</Button>
            </Space>
          </Form>
        ) : null}
      </Modal>
      </> : null}
    </div>
  );
}
