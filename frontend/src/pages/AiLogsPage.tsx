import { SearchOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Collapse, DatePicker, Descriptions, Form, Input, Modal, Select, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, apiErrorMessage } from '../api/client';
import ErrorResult from '../components/ErrorResult';
import JsonBlock from '../components/JsonBlock';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import type { AiLog, JsonRecord } from '../types/api';
import { filtersWithDateRange } from '../utils/filters';
import { compactText, formatTime, numberText } from '../utils/format';

type AiLogFilters = {
  ticket_id?: string;
  email_id?: string;
  call_type?: string;
  provider_name?: string;
  model_name?: string;
  prompt_version?: string;
  status?: string;
  date_range?: unknown;
};

type AiLogDetail = {
  availability?: 'full' | 'metadata_only' | 'expired' | 'corrupt';
  message?: string;
  sections?: Record<string, unknown>;
  associations?: Record<string, unknown>;
  tokens?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  diagnostics?: Record<string, unknown>;
};

function jsonValue(value: unknown): JsonRecord | unknown[] | null {
  if (value === null || value === undefined) return null;
  if (Array.isArray(value)) return value;
  if (typeof value === 'object') return value as JsonRecord;
  return { value };
}

const AI_LABELS: Record<string, string> = {
  ticket_id: '工单', email_id: '邮件', input_tokens: '输入 Token', output_tokens: '输出 Token',
  total_tokens: '总 Token', prompt_tokens: '输入 Token', completion_tokens: '输出 Token',
  intent_type: '邮件意图', handling_level: '处理层级', confidence_score: '置信度',
  extracted_fields: '提取字段', extracted_items: '维修明细', missing_fields: '缺失字段',
  conflict_fields: '冲突字段', evidence: '判断证据', confidence_reasons: '置信度依据',
  manual_review_direction: '人工复核建议', provider: '服务商', model: '模型', status: '状态',
};

function friendlyValue(value: unknown) {
  if (value === null || value === undefined || value === '') return '-';
  if (value === true) return '是';
  if (value === false) return '否';
  if (Array.isArray(value)) return value.length ? <Space wrap>{value.map((item, index) => <Tag key={index}>{typeof item === 'object' ? `第 ${index + 1} 项` : String(item)}</Tag>)}</Space> : '无';
  if (typeof value === 'object') return `${Object.keys(value as object).length} 项内容`;
  return String(value);
}

function StructuredTable({ value, title }: { value: unknown; title: string }) {
  const record = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : { value };
  const rows = Object.entries(record).map(([key, item]) => ({ key, label: AI_LABELS[key] ?? `扩展字段：${key.split('_').join(' ')}`, value: item }));
  return <div><Typography.Title level={5}>{title}</Typography.Title><Table rowKey="key" size="small" pagination={false} dataSource={rows} columns={[
    { title: '项目', dataIndex: 'label', width: 220 },
    { title: '结果', dataIndex: 'value', render: friendlyValue },
  ]} /></div>;
}

export default function AiLogsPage() {
  const navigate = useNavigate();
  const [filters, setFilters] = useState<Record<string, unknown>>({});
  const [page, setPage] = useState(1);
  const [detailId, setDetailId] = useState<number | null>(null);
  const [filterForm] = Form.useForm<AiLogFilters>();
  const logsQuery = useQuery({
    queryKey: ['ai-logs', filters, page],
    queryFn: () => api.aiLogs({ ...filters, page, page_size: 20 }),
  });
  const detailQuery = useQuery({
    queryKey: ['ai-log-detail', detailId],
    queryFn: () => api.aiLogDetail(detailId as number),
    enabled: Boolean(detailId),
  });
  const columns: ColumnsType<AiLog> = [
    { title: 'Trace', dataIndex: 'trace_id', width: 160, ellipsis: true },
    { title: '调用类型', dataIndex: 'call_type', width: 160 },
    { title: 'Provider', dataIndex: 'provider_name', width: 110, render: (value?: string | null) => value || '-' },
    { title: '工单', dataIndex: 'ticket_id', width: 90, render: numberText },
    { title: '邮件', dataIndex: 'email_id', width: 90, render: numberText },
    { title: '模型', dataIndex: 'model_name', width: 170 },
    { title: 'Prompt', dataIndex: 'prompt_version', width: 170 },
    { title: '问题描述', dataIndex: 'problem_description', width: 360, ellipsis: true, render: (value?: string | null) => compactText(value, '-') },
    { title: '状态', dataIndex: 'status', width: 110, render: (value: string) => <StatusTag value={value} /> },
    {
      title: '明细可用性',
      dataIndex: 'availability',
      width: 120,
      render: (value?: AiLog['availability']) => (
        <Tag color={value === 'full' ? 'green' : value === 'metadata_only' ? 'blue' : value === 'expired' ? 'default' : 'red'}>
          {value === 'full' ? '完整' : value === 'metadata_only' ? '仅元数据' : value === 'expired' ? '已过期' : '损坏'}
        </Tag>
      ),
    },
    { title: '置信度', dataIndex: 'confidence_score', width: 90, render: numberText },
    { title: '耗时', dataIndex: 'latency_ms', width: 90, render: (value?: number | null) => (value ? `${value} ms` : '-') },
    { title: '输出摘要', dataIndex: 'output_summary', ellipsis: true, render: (value?: string | null) => compactText(value) },
    { title: '时间', dataIndex: 'created_at', width: 160, render: formatTime },
    {
      title: '明细',
      width: 80,
      render: (_, record) => <Button type="link" size="small" onClick={() => setDetailId(record.id)}>查看</Button>,
    },
  ];

  return (
    <div className="page-stack">
      <PageTitle title="AI 日志" />
      <SectionPanel>
        <Form<AiLogFilters>
          form={filterForm}
          layout="inline"
          className="filter-bar"
          onFinish={(values) => {
            setPage(1);
            setFilters(filtersWithDateRange(values, 'date_range', 'created_start', 'created_end'));
          }}
        >
          <Form.Item name="ticket_id">
            <Input type="number" prefix={<SearchOutlined />} placeholder="工单 ID" />
          </Form.Item>
          <Form.Item name="email_id">
            <Input type="number" placeholder="邮件 ID" />
          </Form.Item>
          <Form.Item name="call_type">
            <Input allowClear placeholder="调用类型" />
          </Form.Item>
          <Form.Item name="provider_name">
            <Input allowClear placeholder="Provider" />
          </Form.Item>
          <Form.Item name="model_name">
            <Input allowClear placeholder="模型" />
          </Form.Item>
          <Form.Item name="prompt_version">
            <Input allowClear placeholder="Prompt" />
          </Form.Item>
          <Form.Item name="status">
            <Select
              allowClear
              placeholder="状态"
              style={{ width: 130 }}
              options={[
                { value: 'success', label: '成功' },
                { value: 'failed', label: '失败' },
                { value: 'low_confidence', label: '低置信度' },
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
                setFilters({});
              }}
            >
              重置
            </Button>
          </Space>
        </Form>
        <Table<AiLog>
          rowKey="id"
          columns={columns}
          dataSource={logsQuery.data?.items ?? []}
          loading={logsQuery.isFetching}
          locale={{
            emptyText: logsQuery.isError
              ? <ErrorResult message={apiErrorMessage(logsQuery.error)} onRetry={() => logsQuery.refetch()} />
              : '暂无 AI 日志'
          }}
          pagination={{ current: page, pageSize: 20, total: logsQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
          expandable={{
            expandedRowRender: (record) => (
              <div className="drawer-stack">
                <Descriptions column={2} size="small" bordered>
                  <Descriptions.Item label="AI环节">{record.ai_stage || '-'}</Descriptions.Item>
                  <Descriptions.Item label="AI动作">{record.ai_action || '-'}</Descriptions.Item>
                  <Descriptions.Item label="具体原因" span={2}>{record.problem_reason || '-'}</Descriptions.Item>
                  <Descriptions.Item label="处理建议" span={2}>{record.resolution_suggestion || '-'}</Descriptions.Item>
                  <Descriptions.Item label="Error Code">{record.error_code || '-'}</Descriptions.Item>
                  <Descriptions.Item label="错误信息">{record.error_message || '-'}</Descriptions.Item>
                  <Descriptions.Item label="输入摘要">{record.input_summary || '-'}</Descriptions.Item>
                  <Descriptions.Item label="输出摘要">{record.output_summary || '-'}</Descriptions.Item>
                  <Descriptions.Item label="错误信息" span={2}>{record.error_message || '-'}</Descriptions.Item>
                  <Descriptions.Item label="JSONL 路径">{record.log_file_path || '-'}</Descriptions.Item>
                  <Descriptions.Item label="JSONL 行号">{record.log_line_no ?? '-'}</Descriptions.Item>
                  <Descriptions.Item label="日志 Hash" span={2}>{record.log_record_hash || '-'}</Descriptions.Item>
                </Descriptions>
                <StructuredTable title="关键结构化结果" value={record.parsed_key_result} />
              </div>
            ),
          }}
        />
      </SectionPanel>
      <Modal width={1000} title={`AI 调用明细 #${detailId ?? ''}`} open={Boolean(detailId)} onCancel={() => setDetailId(null)} footer={null} destroyOnClose>
        {detailQuery.isFetching ? <Typography.Text>加载中...</Typography.Text> : detailQuery.error ? (
          <Alert
            type="error"
            showIcon
            message="AI 明细加载失败"
            description={apiErrorMessage(detailQuery.error)}
            action={<Button size="small" onClick={() => void detailQuery.refetch()}>重试</Button>}
          />
        ) : detailQuery.data ? (() => {
          const detail = detailQuery.data as AiLogDetail;
          const sections = detail.sections || {};
          return (
            <div className="drawer-stack">
              <Alert
                showIcon
                type={detail.availability === 'full' ? 'success' : detail.availability === 'corrupt' ? 'error' : 'warning'}
                message={detail.availability === 'full' ? '完整明细可用' : detail.availability === 'metadata_only' ? '仅元数据可用' : detail.availability === 'expired' ? '完整日志已过期' : '日志内容损坏'}
                description={detail.message}
              />
              <Descriptions bordered size="small" column={2}>
                <Descriptions.Item label="关联工单">{detail.associations?.ticket_id ? <Button type="link" onClick={() => navigate(`/tickets?ticket_id=${detail.associations?.ticket_id}`)}>查看工单 #{String(detail.associations.ticket_id)}</Button> : '-'}</Descriptions.Item>
                <Descriptions.Item label="关联邮件">{detail.associations?.email_id ? <Button type="link" onClick={() => navigate(`/emails?email_id=${detail.associations?.email_id}`)}>查看邮件 #{String(detail.associations.email_id)}</Button> : '-'}</Descriptions.Item>
              </Descriptions>
              <StructuredTable title="Token 使用" value={detail.tokens} />
              <StructuredTable title="调用信息" value={detail.metadata} />
              <StructuredTable title="解析结果" value={sections.parsed_result} />
              <StructuredTable title="诊断与处理建议" value={detail.diagnostics} />
              <Collapse items={[{
                key: 'raw',
                label: '技术原文（展开后可复制排障）',
                children: <Space direction="vertical" style={{ width: '100%' }}>
                  <Typography.Text strong>输入</Typography.Text><JsonBlock value={jsonValue(sections.input)} />
                  <Typography.Text strong>请求</Typography.Text><JsonBlock value={jsonValue(sections.request)} />
                  <Typography.Text strong>响应</Typography.Text><JsonBlock value={jsonValue(sections.response)} />
                  <Typography.Text strong>完整解析结果</Typography.Text><JsonBlock value={jsonValue(sections.parsed_result)} />
                </Space>,
              }]} />
            </div>
          );
        })() : null}
      </Modal>
    </div>
  );
}
