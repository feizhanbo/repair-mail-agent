import { SearchOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Button, Descriptions, Form, Input, Select, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api } from '../api/client';
import JsonBlock from '../components/JsonBlock';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import type { AiLog } from '../types/api';
import { compactText, formatTime, numberText } from '../utils/format';

type AiLogFilters = {
  ticket_id?: number;
  email_id?: number;
  call_type?: string;
  status?: string;
};

export default function AiLogsPage() {
  const [filters, setFilters] = useState<AiLogFilters>({});
  const [page, setPage] = useState(1);
  const logsQuery = useQuery({
    queryKey: ['ai-logs', filters, page],
    queryFn: () => api.aiLogs({ ...filters, page, page_size: 20 }),
  });
  const columns: ColumnsType<AiLog> = [
    { title: 'Trace', dataIndex: 'trace_id', width: 160, ellipsis: true },
    { title: '调用类型', dataIndex: 'call_type', width: 160 },
    { title: 'Provider', dataIndex: 'provider_name', width: 110, render: (value?: string | null) => value || '-' },
    { title: '工单', dataIndex: 'ticket_id', width: 90, render: numberText },
    { title: '邮件', dataIndex: 'email_id', width: 90, render: numberText },
    { title: '模型', dataIndex: 'model_name', width: 170 },
    { title: 'Prompt', dataIndex: 'prompt_version', width: 170 },
    { title: '状态', dataIndex: 'status', width: 110, render: (value: string) => <StatusTag value={value} /> },
    { title: '置信度', dataIndex: 'confidence_score', width: 90, render: numberText },
    { title: '耗时', dataIndex: 'latency_ms', width: 90, render: (value?: number | null) => (value ? `${value} ms` : '-') },
    { title: '输出摘要', dataIndex: 'output_summary', ellipsis: true, render: (value?: string | null) => compactText(value) },
    { title: '时间', dataIndex: 'created_at', width: 160, render: formatTime },
  ];

  return (
    <div className="page-stack">
      <PageTitle title="AI 日志" />
      <SectionPanel>
        <Form<AiLogFilters> layout="inline" className="filter-bar" onFinish={(values) => { setPage(1); setFilters(values); }}>
          <Form.Item name="ticket_id">
            <Input type="number" prefix={<SearchOutlined />} placeholder="工单 ID" />
          </Form.Item>
          <Form.Item name="call_type">
            <Input allowClear placeholder="调用类型" />
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
          <Button htmlType="submit" type="primary">筛选</Button>
        </Form>
        <Table<AiLog>
          rowKey="id"
          columns={columns}
          dataSource={logsQuery.data?.items ?? []}
          loading={logsQuery.isFetching}
          pagination={{ current: page, pageSize: 20, total: logsQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
          expandable={{
            expandedRowRender: (record) => (
              <div className="drawer-stack">
                <Descriptions column={2} size="small" bordered>
                  <Descriptions.Item label="输入摘要">{record.input_summary || '-'}</Descriptions.Item>
                  <Descriptions.Item label="输出摘要">{record.output_summary || '-'}</Descriptions.Item>
                  <Descriptions.Item label="错误信息" span={2}>{record.error_message || '-'}</Descriptions.Item>
                  <Descriptions.Item label="JSONL 路径">{record.log_file_path || '-'}</Descriptions.Item>
                  <Descriptions.Item label="JSONL 行号">{record.log_line_no ?? '-'}</Descriptions.Item>
                  <Descriptions.Item label="日志 Hash" span={2}>{record.log_record_hash || '-'}</Descriptions.Item>
                </Descriptions>
                <div>
                  <Typography.Text strong>关键结构化结果</Typography.Text>
                  <JsonBlock value={record.parsed_key_result} />
                </div>
              </div>
            ),
          }}
        />
      </SectionPanel>
    </div>
  );
}
