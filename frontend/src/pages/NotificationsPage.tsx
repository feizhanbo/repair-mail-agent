import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, DatePicker, Descriptions, Drawer, Form, Input, Select, Space, Table, Tag, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, apiErrorMessage } from '../api/client';
import JsonBlock from '../components/JsonBlock';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import type { NotificationEvent } from '../types/api';
import { filtersWithDateRange } from '../utils/filters';
import { formatTime } from '../utils/format';

type NotificationFilters = {
  delivery_status?: string;
  event_type?: string;
  priority?: string;
  target_type?: string;
  keyword?: string;
  date_range?: unknown;
};

export default function NotificationsPage() {
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState<Record<string, unknown>>({});
  const [selected, setSelected] = useState<NotificationEvent | null>(null);
  const [filterForm] = Form.useForm<NotificationFilters>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ['notifications-page', page, filters],
    queryFn: () => api.notifications({ ...filters, page, page_size: 20 }),
  });
  const readMutation = useMutation({
    mutationFn: (id: number) => api.markNotificationRead(id),
    onSuccess: () => {
      message.success('消息已标记为已读');
      void queryClient.invalidateQueries({ queryKey: ['notifications'] });
      void queryClient.invalidateQueries({ queryKey: ['notifications-page'] });
    },
    onError: (error) => message.error(apiErrorMessage(error)),
  });

  const jumpToTarget = async (record: NotificationEvent) => {
    try {
      if (record.delivery_status === 'unread' || record.delivery_status === 'pending') {
        await api.markNotificationRead(record.id);
      }
      void queryClient.invalidateQueries({ queryKey: ['notifications'] });
      void queryClient.invalidateQueries({ queryKey: ['notifications-page'] });
      if (record.target_type === 'manual_review_task') navigate(`/manual-review?task_id=${record.target_id}`);
      else if (record.target_type === 'repair_ticket' || record.target_type === 'ticket') navigate(`/tickets?ticket_id=${record.target_id}`);
      else if (record.target_type === 'reply') navigate(`/replies?reply_id=${record.target_id}`);
    } catch (error) {
      message.error(apiErrorMessage(error));
    }
  };

  const columns: ColumnsType<NotificationEvent> = [
    { title: '标题', dataIndex: 'title', ellipsis: true },
    { title: '类型', dataIndex: 'event_type', width: 150 },
    { title: '优先级', dataIndex: 'priority', width: 90, render: (value: string) => <Tag color={value === 'high' ? 'orange' : 'blue'}>{value}</Tag> },
    {
      title: '状态',
      dataIndex: 'delivery_status',
      width: 90,
      render: (value: string) => (
        <Tag color={value === 'resolved' ? 'blue' : value === 'read' ? 'default' : 'green'}>
          {value === 'resolved' ? '已解决' : value === 'read' ? '已读' : '未读'}
        </Tag>
      ),
    },
    { title: '时间', dataIndex: 'created_at', width: 160, render: formatTime },
    {
      title: '操作',
      width: 180,
      render: (_, record) => (
        <Space>
          <Button size="small" onClick={() => setSelected(record)}>详情</Button>
          <Button size="small" disabled={record.delivery_status === 'read' || record.delivery_status === 'resolved'} onClick={() => readMutation.mutate(record.id)}>已读</Button>
          <Button size="small" onClick={() => void jumpToTarget(record)}>跳转</Button>
        </Space>
      ),
    },
  ];

  return (
    <div className="page-stack">
      <PageTitle title="站内消息" />
      <SectionPanel>
        <Form<NotificationFilters>
          form={filterForm}
          layout="inline"
          className="filter-bar"
          onFinish={(values) => {
            setPage(1);
            setSelected(null);
            setFilters(filtersWithDateRange(values, 'date_range', 'created_start', 'created_end'));
          }}
        >
          <Form.Item name="delivery_status">
            <Select
              allowClear
              style={{ width: 140 }}
              options={[{ value: 'pending', label: '未读' }, { value: 'read', label: '已读' }, { value: 'resolved', label: '已解决' }]}
              placeholder="消息状态"
            />
          </Form.Item>
          <Form.Item name="event_type">
            <Input allowClear placeholder="事件类型" />
          </Form.Item>
          <Form.Item name="priority">
            <Select
              allowClear
              style={{ width: 120 }}
              placeholder="优先级"
              options={[{ value: 'high', label: '高' }, { value: 'normal', label: '普通' }, { value: 'low', label: '低' }]}
            />
          </Form.Item>
          <Form.Item name="target_type">
            <Input allowClear placeholder="目标类型" />
          </Form.Item>
          <Form.Item name="keyword">
            <Input allowClear placeholder="标题/内容" />
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
                setSelected(null);
                setFilters({});
              }}
            >
              重置
            </Button>
          </Space>
        </Form>
        <Table<NotificationEvent>
          rowKey="id"
          columns={columns}
          dataSource={query.data?.items ?? []}
          loading={query.isFetching}
          pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
        />
      </SectionPanel>
      <Drawer title="消息详情" open={Boolean(selected)} onClose={() => setSelected(null)} width={520}>
        {selected ? (
          <div className="drawer-stack">
            <Descriptions bordered column={1} size="small">
              <Descriptions.Item label="标题">{selected.title}</Descriptions.Item>
              <Descriptions.Item label="内容">{selected.content || '-'}</Descriptions.Item>
              <Descriptions.Item label="事件类型">{selected.event_type}</Descriptions.Item>
              <Descriptions.Item label="目标">{selected.target_type} #{selected.target_id}</Descriptions.Item>
              <Descriptions.Item label="状态">{selected.delivery_status}</Descriptions.Item>
              <Descriptions.Item label="已读时间">{formatTime(selected.read_at)}</Descriptions.Item>
              <Descriptions.Item label="解决时间">{formatTime(selected.resolved_at)}</Descriptions.Item>
              <Descriptions.Item label="创建时间">{formatTime(selected.created_at)}</Descriptions.Item>
            </Descriptions>
            <JsonBlock value={selected.metadata ?? selected.metadata_json} />
          </div>
        ) : null}
      </Drawer>
    </div>
  );
}
