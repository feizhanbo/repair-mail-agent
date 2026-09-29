import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Descriptions, Form, Input, Modal, Space, Switch, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api, apiErrorMessage } from '../api/client';
import PageTitle from '../components/PageTitle';
import { ChangePreview } from '../components/FriendlyPreview';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import { useAuthStore } from '../stores/authStore';
import type { ReplyTemplate, WorkflowStatus, WorkflowTransition } from '../types/api';
import { formatTime } from '../utils/format';
import { hasAnyRole } from '../utils/roles';

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
  const [templateForm] = Form.useForm<TemplateForm>();
  const [editingTemplate, setEditingTemplate] = useState<ReplyTemplate | null>(null);
  const [templateModalOpen, setTemplateModalOpen] = useState(false);
  const systemQuery = useQuery({
    queryKey: ['system-info'],
    queryFn: api.systemInfo,
    enabled: canAdmin,
  });
  const templatesQuery = useQuery({
    queryKey: ['system-reply-templates'],
    queryFn: api.replyTemplates,
    enabled: canAdmin,
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
      {canAdmin ? <>
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
