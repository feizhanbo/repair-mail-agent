import {
  AlertOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  MailOutlined,
  RobotOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Col, Row, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import type { JobRunLog } from '../types/api';
import { formatTime } from '../utils/format';

const columns: ColumnsType<JobRunLog> = [
  { title: '任务', dataIndex: 'job_name' },
  { title: '类型', dataIndex: 'job_type', width: 150 },
  { title: '状态', dataIndex: 'status', width: 110, render: (value: string) => <StatusTag value={value} /> },
  { title: '失败数', dataIndex: 'failed_count', width: 90 },
  { title: '开始时间', dataIndex: 'started_at', width: 160, render: formatTime },
  { title: '错误', dataIndex: 'error_message', ellipsis: true },
];

export default function Dashboard() {
  const navigate = useNavigate();
  const summaryQuery = useQuery({
    queryKey: ['dashboard-summary'],
    queryFn: api.dashboard,
    refetchInterval: 60_000,
  });
  const summary = summaryQuery.data;
  const metrics = [
    { label: '待解析邮件', value: summary?.pending_parse ?? 0, icon: <MailOutlined />, tone: 'blue', path: '/emails' },
    { label: '任务池总数', value: summary?.task_pool_total ?? summary?.manual_review_tasks ?? 0, icon: <TeamOutlined />, tone: 'orange', path: '/manual-review' },
    { label: '我的待处理', value: summary?.current_user_pending_tasks ?? 0, icon: <UserOutlined />, tone: 'cyan', path: '/manual-review?scope=mine' },
    { label: '我的处理中', value: summary?.in_progress_tasks ?? 0, icon: <ClockCircleOutlined />, tone: 'blue', path: '/manual-review?scope=mine&status=claimed' },
    { label: '待客户补充', value: summary?.need_customer_info ?? 0, icon: <ClockCircleOutlined />, tone: 'gold', path: '/tickets' },
    { label: '可导出工单', value: summary?.ready_for_export ?? 0, icon: <CheckCircleOutlined />, tone: 'green', path: '/tickets' },
    { label: '异常工单', value: summary?.error ?? 0, icon: <AlertOutlined />, tone: 'red', path: '/tickets' },
    { label: 'AI 低置信度', value: summary?.ai_low_confidence ?? 0, icon: <RobotOutlined />, tone: 'cyan', path: '/ai-logs' },
  ];

  return (
    <div className="page-stack">
      <PageTitle title="首页看板" />
      <Row gutter={[12, 12]}>
        {metrics.map((metric) => (
          <Col key={metric.label} xs={24} sm={12} md={8} xl={4}>
            <div
              className={`metric-card metric-${metric.tone}`}
              role="button"
              tabIndex={0}
              onClick={() => navigate(metric.path)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') navigate(metric.path);
              }}
            >
              <span className="metric-icon">{metric.icon}</span>
              <Typography.Text type="secondary">{metric.label}</Typography.Text>
              <div className="metric-value">{metric.value}</div>
            </div>
          </Col>
        ))}
      </Row>
      <SectionPanel>
        <div className="section-heading">
          <Typography.Title level={4}>最近异常任务</Typography.Title>
        </div>
        <Table<JobRunLog>
          size="middle"
          rowKey="id"
          loading={summaryQuery.isFetching}
          dataSource={summary?.recent_failed_jobs ?? []}
          columns={columns}
          pagination={false}
        />
      </SectionPanel>
    </div>
  );
}
