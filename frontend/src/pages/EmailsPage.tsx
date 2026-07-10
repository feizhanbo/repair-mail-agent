import { PlusOutlined, ReloadOutlined, SearchOutlined, UploadOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, DatePicker, Descriptions, Drawer, Empty, Form, Input, Modal, Select, Space, Table, Tabs, Typography, Upload, message } from 'antd';
import type { UploadProps } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api, apiErrorMessage } from '../api/client';
import JsonBlock from '../components/JsonBlock';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import type { EmailDetail, EmailFlowTrace, EmailIngestRequest, EmailItem, ParseResult } from '../types/api';
import { filtersWithDateRange } from '../utils/filters';
import { compactText, formatTime, numberText } from '../utils/format';

type EmailFilters = {
  subject?: string;
  from_address?: string;
  message_id?: string;
  parse_status?: string;
  intent_type?: string;
  date_range?: unknown;
};

export default function EmailsPage() {
  const [filters, setFilters] = useState<Record<string, unknown>>({});
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [ingestOpen, setIngestOpen] = useState(false);
  const [filterForm] = Form.useForm<EmailFilters>();
  const queryClient = useQueryClient();
  const handleMutationError = (error: unknown) => message.error(apiErrorMessage(error));
  const emailsQuery = useQuery({
    queryKey: ['emails', filters, page],
    queryFn: () => api.emails({ ...filters, page, page_size: 20 }),
  });
  const detailQuery = useQuery({
    queryKey: ['email-detail', selectedId],
    queryFn: () => api.emailDetail(selectedId as number),
    enabled: Boolean(selectedId),
  });
  const flowTraceQuery = useQuery({
    queryKey: ['email-flow-trace', selectedId],
    queryFn: () => api.emailFlowTrace(selectedId as number),
    enabled: Boolean(selectedId),
  });
  const ingestMutation = useMutation({
    mutationFn: api.ingestEmail,
    onSuccess: () => {
      message.success('邮件已入库');
      setIngestOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['emails'] });
      void queryClient.invalidateQueries({ queryKey: ['tickets'] });
    },
    onError: handleMutationError,
  });
  const ingestEmlMutation = useMutation({
    mutationFn: (file: File) => api.ingestEmlFile(file),
    onSuccess: (result) => {
      message.success('EML 邮件已入库');
      setSelectedId(result.email?.id ?? null);
      void queryClient.invalidateQueries({ queryKey: ['emails'] });
      void queryClient.invalidateQueries({ queryKey: ['tickets'] });
      void queryClient.invalidateQueries({ queryKey: ['manual-tasks'] });
      void queryClient.invalidateQueries({ queryKey: ['email-flow-trace', result.email?.id] });
    },
    onError: handleMutationError,
  });
  const reparseMutation = useMutation({
    mutationFn: (id: number) => api.reparseEmail(id),
    onSuccess: () => {
      message.success('重解析已完成');
      void queryClient.invalidateQueries({ queryKey: ['email-detail', selectedId] });
      void queryClient.invalidateQueries({ queryKey: ['email-flow-trace', selectedId] });
      void queryClient.invalidateQueries({ queryKey: ['tickets'] });
    },
    onError: handleMutationError,
  });

  const columns: ColumnsType<EmailItem> = [
    { title: '主题', dataIndex: 'subject', ellipsis: true, render: (value?: string) => value || '-' },
    { title: '发件人', dataIndex: 'from_address', ellipsis: true, width: 220 },
    { title: '意图', dataIndex: 'intent_type', width: 130, render: (value?: string) => <StatusTag value={value} /> },
    { title: '解析', dataIndex: 'parse_status', width: 110, render: (value: string) => <StatusTag value={value} kind="parse" /> },
    { title: '线程', dataIndex: 'thread_id', width: 90, render: numberText },
    { title: '收信时间', dataIndex: 'received_at', width: 160, render: formatTime },
    {
      title: '操作',
      width: 130,
      render: (_, record) => (
        <Space>
          <Button type="link" size="small" onClick={() => setSelectedId(record.id)}>
            详情
          </Button>
          <Button
            type="link"
            size="small"
            icon={<ReloadOutlined />}
            onClick={() => Modal.confirm({
              title: '确认重新解析该邮件？',
              content: '该操作会生成新的解析候选，并可能更新关联工单。',
              okText: '确认',
              cancelText: '取消',
              onOk: () => reparseMutation.mutate(record.id),
            })}
          />
        </Space>
      ),
    },
  ];
  const emlUploadProps: UploadProps = {
    accept: '.eml,message/rfc822',
    showUploadList: false,
    beforeUpload: (file) => {
      ingestEmlMutation.mutate(file);
      return Upload.LIST_IGNORE;
    },
  };

  return (
    <div className="page-stack">
      <PageTitle
        title="邮件中心"
        extra={(
          <Space wrap>
            <Upload {...emlUploadProps}>
              <Button icon={<UploadOutlined />} loading={ingestEmlMutation.isPending}>
                上传 EML
              </Button>
            </Upload>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setIngestOpen(true)}>
              手动入库
            </Button>
          </Space>
        )}
      />
      <SectionPanel>
        <Form<EmailFilters>
          form={filterForm}
          layout="inline"
          className="filter-bar"
          onFinish={(values) => {
            setPage(1);
            setSelectedId(null);
            setFilters(filtersWithDateRange(values, 'date_range', 'received_start', 'received_end'));
          }}
        >
          <Form.Item name="subject">
            <Input allowClear prefix={<SearchOutlined />} placeholder="主题" />
          </Form.Item>
          <Form.Item name="from_address">
            <Input allowClear placeholder="发件人" />
          </Form.Item>
          <Form.Item name="message_id">
            <Input allowClear placeholder="Message-ID" />
          </Form.Item>
          <Form.Item name="intent_type">
            <Select
              allowClear
              placeholder="邮件意图"
              style={{ width: 150 }}
              options={[
                { value: 'new_repair', label: '新报修' },
                { value: 'customer_reply', label: '客户补充' },
                { value: 'internal_forward', label: '内部转发' },
                { value: 'irrelevant', label: '无关' },
                { value: 'unknown', label: '未知' },
              ]}
            />
          </Form.Item>
          <Form.Item name="parse_status">
            <Select
              allowClear
              placeholder="解析状态"
              style={{ width: 140 }}
              options={[
                { value: 'pending', label: '待解析' },
                { value: 'parsed', label: '已解析' },
                { value: 'skipped', label: '已跳过' },
              ]}
            />
          </Form.Item>
          <Form.Item name="date_range">
            <DatePicker.RangePicker allowClear />
          </Form.Item>
          <Space>
            <Button htmlType="submit" type="primary">筛选</Button>
            <Button
              onClick={() => {
                filterForm.resetFields();
                setPage(1);
                setSelectedId(null);
                setFilters({});
              }}
            >
              重置
            </Button>
          </Space>
        </Form>
        <Table<EmailItem>
          rowKey="id"
          columns={columns}
          dataSource={emailsQuery.data?.items ?? []}
          loading={emailsQuery.isFetching}
          locale={{ emptyText: emailsQuery.isError ? '邮件加载失败' : '暂无邮件' }}
          pagination={{ current: page, pageSize: 20, total: emailsQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
        />
      </SectionPanel>
      <Drawer width={920} title="邮件详情" open={Boolean(selectedId)} onClose={() => setSelectedId(null)}>
        {detailQuery.data ? (
          <Tabs
            items={[
              {
                key: 'mail',
                label: '邮件',
                children: <EmailDetailView detail={detailQuery.data} />,
              },
              {
                key: 'parse',
                label: `解析候选(${detailQuery.data.parse_results.length})`,
                children: <ParseResultsView parseResults={detailQuery.data.parse_results} />,
              },
              {
                key: 'flow',
                label: '链路追踪',
                children: <FlowTraceView trace={flowTraceQuery.data} loading={flowTraceQuery.isFetching} />,
              },
            ]}
          />
        ) : null}
      </Drawer>
      <Modal title="手动邮件入库" open={ingestOpen} onCancel={() => setIngestOpen(false)} footer={null} destroyOnClose>
        <Form<EmailIngestRequest>
          layout="vertical"
          initialValues={{ mailbox_account: 'manual', folder_name: 'INBOX' }}
          onFinish={(values) => ingestMutation.mutate(values)}
        >
          <Form.Item label="邮箱账号" name="mailbox_account" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item label="发件人" name="from_address" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item label="收件人" name="to_addresses">
            <Input />
          </Form.Item>
          <Form.Item label="主题" name="subject">
            <Input />
          </Form.Item>
          <Form.Item label="正文" name="text_body">
            <Input.TextArea rows={7} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={ingestMutation.isPending}>
            入库
          </Button>
        </Form>
      </Modal>
    </div>
  );
}

function EmailDetailView({ detail }: { detail: EmailDetail }) {
  return (
    <div className="drawer-stack">
      <Descriptions column={1} size="small" bordered>
        <Descriptions.Item label="主题">{detail.email.subject || '-'}</Descriptions.Item>
        <Descriptions.Item label="发件人">{detail.email.from_address}</Descriptions.Item>
        <Descriptions.Item label="收件人">{detail.email.to_addresses || '-'}</Descriptions.Item>
        <Descriptions.Item label="抄送">{detail.email.cc_addresses || '-'}</Descriptions.Item>
        <Descriptions.Item label="Message-ID">{detail.email.message_id || '-'}</Descriptions.Item>
        <Descriptions.Item label="解析状态"><StatusTag value={detail.email.parse_status} kind="parse" /></Descriptions.Item>
        <Descriptions.Item label="正文">{compactText(detail.email.clean_body, '-')}</Descriptions.Item>
      </Descriptions>
      <div>
        <Typography.Title level={5}>附件</Typography.Title>
        <Table
          size="small"
          rowKey="id"
          dataSource={detail.attachments}
          pagination={false}
          locale={{ emptyText: '暂无附件' }}
          columns={[
            { title: '文件名', dataIndex: 'file_name', ellipsis: true },
            { title: '类型', dataIndex: 'content_type', width: 170, ellipsis: true },
            { title: '大小', dataIndex: 'file_size', width: 100, render: numberText },
            { title: '状态', dataIndex: 'parse_status', width: 110, render: (value: string) => <StatusTag value={value} kind="parse" /> },
          ]}
        />
      </div>
    </div>
  );
}

function ParseResultsView({ parseResults }: { parseResults: ParseResult[] }) {
  return (
    <Table<ParseResult>
      size="small"
      rowKey="id"
      dataSource={parseResults}
      pagination={false}
      scroll={{ x: 780 }}
      columns={[
        { title: '解析器', dataIndex: 'parser_type', width: 90 },
        { title: '意图', dataIndex: 'intent_type', width: 120, render: (value?: string) => <StatusTag value={value} /> },
        { title: '置信度', dataIndex: 'confidence_score', width: 90, render: numberText },
        { title: '应用状态', dataIndex: 'apply_status', width: 120, render: (value: string) => <StatusTag value={value} /> },
        { title: '缺失字段', dataIndex: 'missing_fields', width: 220, render: (value) => <JsonBlock value={value} /> },
        { title: '冲突字段', dataIndex: 'conflict_fields', width: 220, render: (value) => <JsonBlock value={value} /> },
        { title: '证据', dataIndex: 'evidence', width: 260, render: (value) => <JsonBlock value={value} /> },
      ]}
    />
  );
}

function FlowTraceView({ trace, loading }: { trace?: EmailFlowTrace; loading: boolean }) {
  if (loading && !trace) {
    return <Table size="small" loading dataSource={[]} columns={[{ title: '链路节点', dataIndex: 'name' }]} pagination={false} />;
  }
  if (!trace) {
    return <Empty description="暂无链路数据" />;
  }

  const counters = [
    { label: '工单', value: trace.tickets.length },
    { label: '明细', value: trace.ticket_items.length },
    { label: '人工任务', value: trace.manual_review_tasks.length },
    { label: '回复', value: trace.reply_records.length },
    { label: 'AI 调用', value: trace.ai_call_logs.length },
    { label: '通知', value: trace.notification_events.length },
  ];

  return (
    <div className="drawer-stack">
      <Descriptions column={3} size="small" bordered>
        <Descriptions.Item label="发送模式">{trace.runtime_config.reply_send_mode}</Descriptions.Item>
        <Descriptions.Item label="自动发送">{trace.runtime_config.auto_send_enabled ? '开启' : '关闭'}</Descriptions.Item>
        <Descriptions.Item label="置信度阈值">{numberText(trace.runtime_config.confidence_threshold)}</Descriptions.Item>
        {counters.map((item) => (
          <Descriptions.Item key={item.label} label={item.label}>{item.value}</Descriptions.Item>
        ))}
      </Descriptions>

      <div>
        <Typography.Title level={5}>邮件与工单关联</Typography.Title>
        <Table
          size="small"
          rowKey="id"
          dataSource={trace.email_ticket_links}
          pagination={false}
          locale={{ emptyText: '暂无关联' }}
          columns={[
            { title: '工单 ID', dataIndex: 'ticket_id', width: 100 },
            { title: '类型', dataIndex: 'link_type', width: 140, render: (value?: string) => <StatusTag value={value} /> },
            { title: '原因', dataIndex: 'link_reason', render: (value?: string) => compactText(value, '-') },
            { title: '时间', dataIndex: 'created_at', width: 160, render: formatTime },
          ]}
        />
      </div>

      <div>
        <Typography.Title level={5}>工单</Typography.Title>
        <Table
          size="small"
          rowKey="id"
          dataSource={trace.tickets}
          pagination={false}
          scroll={{ x: 780 }}
          locale={{ emptyText: '暂无工单' }}
          columns={[
            { title: '工单号', dataIndex: 'ticket_no', width: 140 },
            { title: '状态', dataIndex: 'current_status_code', width: 150, render: (value?: string) => <StatusTag value={value} /> },
            { title: '客户', dataIndex: 'customer_name', width: 160, ellipsis: true },
            { title: '处理人', dataIndex: 'assigned_user_id', width: 100, render: numberText },
            { title: '置信度', dataIndex: 'confidence_score', width: 90, render: numberText },
            { title: '问题描述', dataIndex: 'problem_description', ellipsis: true, render: (value?: string) => compactText(value, '-') },
          ]}
        />
      </div>

      <div>
        <Typography.Title level={5}>工单明细</Typography.Title>
        <Table
          size="small"
          rowKey="id"
          dataSource={trace.ticket_items}
          pagination={false}
          scroll={{ x: 780 }}
          locale={{ emptyText: '暂无明细' }}
          columns={[
            { title: 'SN', dataIndex: 'sn', width: 160 },
            { title: '物料', dataIndex: 'material_code', width: 140 },
            { title: '校验', dataIndex: 'validation_status', width: 110, render: (value?: string) => <StatusTag value={value} /> },
            { title: '故障描述', dataIndex: 'failure_description', ellipsis: true, render: (value?: string) => compactText(value, '-') },
          ]}
        />
      </div>

      <div>
        <Typography.Title level={5}>人工任务</Typography.Title>
        <Table
          size="small"
          rowKey="id"
          dataSource={trace.manual_review_tasks}
          pagination={false}
          scroll={{ x: 780 }}
          locale={{ emptyText: '暂无人工任务' }}
          columns={[
            { title: '类型', dataIndex: 'task_type', width: 150, render: (value?: string) => <StatusTag value={value} /> },
            { title: '状态', dataIndex: 'status', width: 110, render: (value?: string) => <StatusTag value={value} /> },
            { title: '优先级', dataIndex: 'priority', width: 90, render: (value?: string) => <StatusTag value={value} /> },
            { title: '处理人', dataIndex: 'assigned_user_id', width: 100, render: numberText },
            { title: '原因', dataIndex: 'trigger_reason', ellipsis: true, render: (value?: string) => compactText(value, '-') },
          ]}
        />
      </div>

      <div>
        <Typography.Title level={5}>回复记录</Typography.Title>
        <Table
          size="small"
          rowKey="id"
          dataSource={trace.reply_records}
          pagination={false}
          scroll={{ x: 780 }}
          locale={{ emptyText: '暂无回复' }}
          columns={[
            { title: '类型', dataIndex: 'reply_type', width: 120, render: (value?: string) => <StatusTag value={value} /> },
            { title: '审核', dataIndex: 'review_status', width: 120, render: (value?: string) => <StatusTag value={value} /> },
            { title: '发送', dataIndex: 'send_status', width: 120, render: (value?: string) => <StatusTag value={value} /> },
            { title: '收件人', dataIndex: 'to_addresses', width: 180, ellipsis: true },
            { title: '错误', dataIndex: 'error_message', ellipsis: true, render: (value?: string) => compactText(value, '-') },
          ]}
        />
      </div>

      <div>
        <Typography.Title level={5}>AI 调用</Typography.Title>
        <Table
          size="small"
          rowKey="id"
          dataSource={trace.ai_call_logs}
          pagination={false}
          scroll={{ x: 780 }}
          locale={{ emptyText: '暂无 AI 调用' }}
          columns={[
            { title: '类型', dataIndex: 'call_type', width: 140 },
            { title: '状态', dataIndex: 'status', width: 110, render: (value?: string) => <StatusTag value={value} /> },
            { title: '模型', dataIndex: 'model_name', width: 170, ellipsis: true },
            { title: '置信度', dataIndex: 'confidence_score', width: 90, render: numberText },
            { title: '错误', dataIndex: 'error_message', ellipsis: true, render: (value?: string) => compactText(value, '-') },
          ]}
        />
      </div>

      <div>
        <Typography.Title level={5}>状态流转</Typography.Title>
        <Table
          size="small"
          rowKey="id"
          dataSource={trace.ticket_status_logs}
          pagination={false}
          scroll={{ x: 780 }}
          locale={{ emptyText: '暂无状态流转' }}
          columns={[
            { title: '从', dataIndex: 'from_status_code', width: 130, render: (value?: string) => <StatusTag value={value} /> },
            { title: '到', dataIndex: 'to_status_code', width: 130, render: (value?: string) => <StatusTag value={value} /> },
            { title: '事件', dataIndex: 'trigger_event', width: 150 },
            { title: '原因', dataIndex: 'reason', ellipsis: true, render: (value?: string) => compactText(value, '-') },
            { title: '时间', dataIndex: 'created_at', width: 160, render: formatTime },
          ]}
        />
      </div>

      <div>
        <Typography.Title level={5}>操作日志</Typography.Title>
        <Table
          size="small"
          rowKey="id"
          dataSource={trace.operation_logs}
          pagination={false}
          scroll={{ x: 780 }}
          locale={{ emptyText: '暂无操作日志' }}
          columns={[
            { title: '操作', dataIndex: 'operation_type', width: 170 },
            { title: '对象', dataIndex: 'target_type', width: 140 },
            { title: '说明', dataIndex: 'description', ellipsis: true, render: (value?: string) => compactText(value, '-') },
            { title: '时间', dataIndex: 'created_at', width: 160, render: formatTime },
          ]}
        />
      </div>
    </div>
  );
}
