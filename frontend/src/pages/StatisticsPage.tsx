import {
  CheckCircleOutlined,
  FileTextOutlined,
  MailOutlined,
  RobotOutlined,
  SyncOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { DatePicker, Progress, Row, Col, Select, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import dayjs, { type Dayjs } from 'dayjs';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import type { StatisticsTrendItem, UserProcessingStat } from '../types/api';

const { RangePicker } = DatePicker;

type Period = 'week' | 'month' | 'year';

export default function StatisticsPage() {
  const navigate = useNavigate();
  const [period, setPeriod] = useState<Period>('week');
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>(null);
  const params = {
    period,
    start_date: range?.[0]?.format('YYYY-MM-DD'),
    end_date: range?.[1]?.format('YYYY-MM-DD'),
  };
  const query = useQuery({
    queryKey: ['statistics-summary', params],
    queryFn: () => api.statistics(params),
  });
  const data = query.data;

  const metrics = [
    { label: '邮件数', value: data?.email_count ?? 0, icon: <MailOutlined />, tone: 'blue', path: '/emails' },
    { label: '工单数', value: data?.ticket_count ?? 0, icon: <FileTextOutlined />, tone: 'orange', path: '/tickets' },
    { label: '完成数', value: data?.completed_count ?? 0, icon: <CheckCircleOutlined />, tone: 'green', path: '/tickets?status_code=ready_for_export' },
    { label: '重解析次数', value: data?.reparse_count ?? 0, icon: <SyncOutlined />, tone: 'gold', path: '/emails' },
    { label: 'AI 成功率', value: `${data?.ai_success_rate ?? 0}%`, icon: <RobotOutlined />, tone: 'cyan', path: '/ai-logs' },
    { label: '自动回复率', value: `${data?.auto_reply_rate ?? 0}%`, icon: <UserOutlined />, tone: 'red', path: '/replies' },
    { label: '人工介入率', value: `${data?.manual_intervention_rate ?? 0}%`, icon: <UserOutlined />, tone: 'gold', path: '/manual-review' },
    { label: '任务池', value: data?.task_pool_total ?? 0, icon: <UserOutlined />, tone: 'orange', path: '/manual-review' },
    { label: '待客户补充', value: data?.need_customer_info ?? 0, icon: <MailOutlined />, tone: 'blue', path: '/tickets?status_code=need_customer_info' },
    { label: '异常工单', value: data?.error_ticket_count ?? 0, icon: <SyncOutlined />, tone: 'red', path: '/tickets?status_code=error' },
  ];

  const trendColumns: ColumnsType<StatisticsTrendItem> = [
    { title: '日期', dataIndex: 'date' },
    { title: '邮件数', dataIndex: 'emails', width: 120 },
    { title: '工单数', dataIndex: 'tickets', width: 120 },
    { title: '完成数', dataIndex: 'completed', width: 120 },
  ];
  const userColumns: ColumnsType<UserProcessingStat> = [
    { title: '处理人', dataIndex: 'real_name', render: (_, record) => `${record.real_name}（${record.username}）` },
    { title: '完成任务数', dataIndex: 'resolved_count', width: 140 },
    {
      title: '占比',
      width: 180,
      render: (_, record) => {
        const total = data?.user_processing.reduce((sum, item) => sum + item.resolved_count, 0) || 0;
        const percent = total ? Math.round((record.resolved_count / total) * 100) : 0;
        return <Progress percent={percent} size="small" />;
      },
    },
  ];

  return (
    <div className="page-stack">
      <PageTitle title="统计分析" />
      <SectionPanel>
        <Space wrap>
          <Select
            value={period}
            style={{ width: 120 }}
            options={[
              { value: 'week', label: '本周' },
              { value: 'month', label: '本月' },
              { value: 'year', label: '本年' },
            ]}
            onChange={(value) => setPeriod(value)}
          />
          <RangePicker
            value={range}
            onChange={(values) => {
              if (values?.[0] && values[1]) setRange([values[0], values[1]]);
              else setRange(null);
            }}
            disabledDate={(current) => current.isAfter(dayjs(), 'day')}
          />
        </Space>
      </SectionPanel>
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
          <Typography.Title level={4}>趋势数据</Typography.Title>
          <Typography.Text type="secondary">
            {data ? `${data.start_date} 至 ${data.end_date}` : '-'}
          </Typography.Text>
        </div>
        <Table<StatisticsTrendItem>
          rowKey="date"
          loading={query.isFetching}
          columns={trendColumns}
          dataSource={data?.trend ?? []}
          pagination={false}
        />
      </SectionPanel>
      <SectionPanel>
        <div className="section-heading">
          <Typography.Title level={4}>用户处理量</Typography.Title>
        </div>
        <Table<UserProcessingStat>
          rowKey="user_id"
          loading={query.isFetching}
          columns={userColumns}
          dataSource={data?.user_processing ?? []}
          pagination={false}
        />
      </SectionPanel>
    </div>
  );
}
