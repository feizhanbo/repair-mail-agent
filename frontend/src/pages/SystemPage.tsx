import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Descriptions, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useEffect, useState } from 'react';
import { api, apiErrorMessage } from '../api/client';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import type { ReplyTemplate, WorkflowStatus, WorkflowTransition } from '../types/api';
import { formatTime } from '../utils/format';

type ConfigForm = {
  auto_send_enabled: boolean;
  reply_send_mode: 'human_review' | 'auto_send';
  auto_send_min_confidence: number;
  confidence_threshold: number;
  max_follow_up: number;
};

type TemplateForm = {
  template_name: string;
  subject_template?: string | null;
  body_template: string;
  enabled: boolean;
};

export default function SystemPage() {
  const queryClient = useQueryClient();
  const [configForm] = Form.useForm<ConfigForm>();
  const [templateForm] = Form.useForm<TemplateForm>();
  const [editingTemplate, setEditingTemplate] = useState<ReplyTemplate | null>(null);
  const systemQuery = useQuery({
    queryKey: ['system-info'],
    queryFn: api.systemInfo,
  });
  const configQuery = useQuery({
    queryKey: ['system-config'],
    queryFn: api.systemConfig,
  });
  const templatesQuery = useQuery({
    queryKey: ['system-reply-templates'],
    queryFn: api.replyTemplates,
  });
  const configMutation = useMutation({
    mutationFn: (values: ConfigForm) => api.updateSystemConfig(values),
    onSuccess: () => {
      message.success('系统配置已保存');
      void queryClient.invalidateQueries({ queryKey: ['system-info'] });
      void queryClient.invalidateQueries({ queryKey: ['system-config'] });
    },
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const templateMutation = useMutation({
    mutationFn: ({ id, values }: { id: number; values: TemplateForm }) => api.updateReplyTemplate(id, values),
    onSuccess: () => {
      message.success('回复话术已保存');
      setEditingTemplate(null);
      templateForm.resetFields();
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
        reply_send_mode: configQuery.data.reply_send_mode,
        auto_send_min_confidence: configQuery.data.auto_send_min_confidence,
        confidence_threshold: configQuery.data.confidence_threshold,
        max_follow_up: configQuery.data.max_follow_up,
      });
    }
  }, [configForm, configQuery.data]);

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
      width: 90,
      render: (_, record) => (
        <Button
          size="small"
          onClick={() => {
            setEditingTemplate(record);
            templateForm.setFieldsValue({
              template_name: record.template_name,
              subject_template: record.subject_template ?? undefined,
              body_template: record.body_template,
              enabled: record.enabled,
            });
          }}
        >
          编辑
        </Button>
      ),
    },
  ];

  return (
    <div className="page-stack">
      <PageTitle title="系统配置" />
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
        <Form<ConfigForm>
          form={configForm}
          layout="inline"
          className="filter-bar"
          onFinish={(values) => configMutation.mutate(values)}
        >
          <Form.Item label="自动发送" name="auto_send_enabled" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item label="发送模式" name="reply_send_mode" rules={[{ required: true }]}>
            <Select
              style={{ width: 180 }}
              options={[
                { value: 'human_review', label: '人工确认后发送' },
                { value: 'auto_send', label: '满足条件自动发送' },
              ]}
            />
          </Form.Item>
          <Form.Item label="置信度阈值" name="confidence_threshold" rules={[{ required: true }]}>
            <InputNumber min={0} max={1} step={0.01} precision={2} />
          </Form.Item>
          <Form.Item label="自动发送安全阈值" name="auto_send_min_confidence" rules={[{ required: true }]}>
            <InputNumber min={0} max={1} step={0.01} precision={2} />
          </Form.Item>
          <Form.Item label="追问上限" name="max_follow_up" rules={[{ required: true }]}>
            <InputNumber min={0} max={20} precision={0} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={configMutation.isPending || configQuery.isFetching}>
            保存
          </Button>
        </Form>
      </SectionPanel>
      <SectionPanel>
        <div className="section-heading">
          <Typography.Title level={4}>回复话术</Typography.Title>
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
          <Descriptions.Item label="自动发送">{info?.auto_send_enabled ? '开启' : '关闭'}</Descriptions.Item>
          <Descriptions.Item label="发送模式">{info?.reply_send_mode === 'auto_send' ? '满足条件自动发送' : '人工确认后发送'}</Descriptions.Item>
          <Descriptions.Item label="追问上限">{info?.max_follow_up ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="置信度阈值">{info?.confidence_threshold ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="自动发送安全阈值">{info?.auto_send_min_confidence ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="AI 状态">
            <Tag color={integrations.ai_configured ? 'green' : 'default'}>{integrations.ai_configured ? '已配置' : '未配置'}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label="AI Provider">{String(integrations.ai_provider ?? '-')}</Descriptions.Item>
          <Descriptions.Item label="AI 模型">{String(integrations.ai_model ?? '-')}</Descriptions.Item>
          <Descriptions.Item label="AI Base URL">{String(integrations.ai_base_url ?? '-')}</Descriptions.Item>
          <Descriptions.Item label="Prompt 版本">{String(integrations.ai_prompt_version ?? '-')}</Descriptions.Item>
          <Descriptions.Item label="AI 超时">{integrations.ai_timeout_seconds ? `${String(integrations.ai_timeout_seconds)}s` : '-'}</Descriptions.Item>
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
      <Modal title="编辑回复话术" open={Boolean(editingTemplate)} onCancel={() => setEditingTemplate(null)} footer={null} destroyOnClose>
        {editingTemplate ? (
          <Form<TemplateForm>
            form={templateForm}
            layout="vertical"
            onFinish={(values) => templateMutation.mutate({ id: editingTemplate.id, values })}
          >
            <Form.Item label="名称" name="template_name" rules={[{ required: true }]}>
              <Input />
            </Form.Item>
            <Form.Item label="主题模板" name="subject_template">
              <Input />
            </Form.Item>
            <Form.Item label="正文模板" name="body_template" rules={[{ required: true }]}>
              <Input.TextArea rows={10} />
            </Form.Item>
            <Form.Item label="启用" name="enabled" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={templateMutation.isPending}>保存</Button>
              <Button onClick={() => setEditingTemplate(null)}>取消</Button>
            </Space>
          </Form>
        ) : null}
      </Modal>
    </div>
  );
}
