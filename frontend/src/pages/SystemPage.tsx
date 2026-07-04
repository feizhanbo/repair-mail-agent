import { useQuery } from '@tanstack/react-query';
import { Descriptions, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../api/client';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import type { WorkflowStatus, WorkflowTransition } from '../types/api';

export default function SystemPage() {
  const systemQuery = useQuery({
    queryKey: ['system-info'],
    queryFn: api.systemInfo,
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

  return (
    <div className="page-stack">
      <PageTitle title="系统配置" />
      <SectionPanel>
        <Descriptions column={3} size="small" bordered>
          <Descriptions.Item label="应用">{info?.app ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="环境">{info?.env ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="自动发送">{info?.auto_send_enabled ? '开启' : '关闭'}</Descriptions.Item>
          <Descriptions.Item label="追问上限">{info?.max_follow_up ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="置信度阈值">{info?.confidence_threshold ?? '-'}</Descriptions.Item>
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
    </div>
  );
}
