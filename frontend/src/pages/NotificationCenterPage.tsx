import {
  BellOutlined,
  CheckCircleOutlined,
  FileTextOutlined,
  MailOutlined,
  MessageOutlined,
  RobotOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Badge, Button, Empty, List, Space, Tabs, Tag, Typography, message } from 'antd';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, apiErrorMessage } from '../api/client';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import type { NotificationEvent } from '../types/api';
import { formatTime } from '../utils/format';

function eventIcon(eventType: string) {
  if (eventType.includes('email')) return <MailOutlined />;
  if (eventType.includes('ticket')) return <FileTextOutlined />;
  if (eventType.includes('ai')) return <RobotOutlined />;
  if (eventType.includes('reply')) return <MessageOutlined />;
  if (eventType.includes('review')) return <CheckCircleOutlined />;
  return <BellOutlined />;
}

function targetPath(record: NotificationEvent) {
  if (record.target_type === 'manual_review_task') return `/manual-review?task_id=${record.target_id}`;
  if (record.target_type === 'repair_ticket' || record.target_type === 'ticket') return `/tickets?ticket_id=${record.target_id}`;
  if (record.target_type === 'reply') return `/replies?reply_id=${record.target_id}`;
  return '/notifications';
}

export default function NotificationCenterPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<'all' | 'pending' | 'read' | 'resolved'>('all');
  const params = { page: 1, page_size: 50, delivery_status: activeTab === 'all' ? undefined : activeTab };
  const query = useQuery({
    queryKey: ['notification-center', params],
    queryFn: () => api.notifications(params),
  });
  const readMutation = useMutation({
    mutationFn: (id: number) => api.markNotificationRead(id),
    onSuccess: () => {
      message.success('已标记为已读');
      void queryClient.invalidateQueries({ queryKey: ['notification-center'] });
      void queryClient.invalidateQueries({ queryKey: ['notifications'] });
    },
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const list = query.data?.items ?? [];
  const unread = list.filter((item) => item.delivery_status === 'unread' || item.delivery_status === 'pending').length;
  const jumpToTarget = async (item: NotificationEvent) => {
    try {
      if (item.delivery_status === 'unread' || item.delivery_status === 'pending') {
        await api.markNotificationRead(item.id);
      }
      void queryClient.invalidateQueries({ queryKey: ['notification-center'] });
      void queryClient.invalidateQueries({ queryKey: ['notifications'] });
      navigate(targetPath(item));
    } catch (error) {
      message.error(apiErrorMessage(error));
    }
  };

  return (
    <div className="page-stack">
      <PageTitle
        title="通知中心"
        extra={(
          <Badge count={unread} size="small">
            <Button onClick={() => navigate('/notifications')}>查看站内消息</Button>
          </Badge>
        )}
      />
      <SectionPanel className="notification-center-panel">
        <Tabs
          activeKey={activeTab}
          onChange={(value) => setActiveTab(value as 'all' | 'pending' | 'read' | 'resolved')}
          items={[
            { key: 'all', label: '全部通知' },
            { key: 'pending', label: '未读' },
            { key: 'read', label: '已读' },
            { key: 'resolved', label: '已解决' },
          ]}
        />
        {list.length ? (
          <List
            loading={query.isFetching}
            dataSource={list}
            renderItem={(item) => (
              <List.Item
                className={item.delivery_status !== 'unread' ? 'notification-row is-read' : 'notification-row'}
                actions={[
                  <Button key="detail" type="link" onClick={() => void jumpToTarget(item)}>跳转</Button>,
                  <Button
                    key="read"
                    type="link"
                    disabled={item.delivery_status === 'read' || item.delivery_status === 'resolved'}
                    onClick={() => readMutation.mutate(item.id)}
                  >
                    已读
                  </Button>,
                ]}
              >
                <List.Item.Meta
                  avatar={<span className="notification-icon">{eventIcon(item.event_type)}</span>}
                  title={(
                    <Space wrap>
                      <Typography.Text strong>{item.title}</Typography.Text>
                      <Tag color={item.priority === 'high' ? 'orange' : 'blue'}>{item.priority}</Tag>
                      <Tag>{item.event_type}</Tag>
                      <Tag color={item.delivery_status === 'resolved' ? 'blue' : item.delivery_status === 'read' ? 'default' : 'green'}>
                        {item.delivery_status === 'resolved' ? '已解决' : item.delivery_status === 'read' ? '已读' : '未读'}
                      </Tag>
                    </Space>
                  )}
                  description={(
                    <div className="notification-description">
                      <Typography.Paragraph ellipsis={{ rows: 2 }}>{item.content || '-'}</Typography.Paragraph>
                      <Typography.Text type="secondary">{formatTime(item.created_at)}</Typography.Text>
                      {item.delivery_status === 'resolved' ? (
                        <Typography.Text type="secondary">解决于 {formatTime(item.resolved_at)}</Typography.Text>
                      ) : null}
                    </div>
                  )}
                />
              </List.Item>
            )}
          />
        ) : (
          <Empty description={query.isFetching ? '加载中' : '暂无通知'} />
        )}
      </SectionPanel>
    </div>
  );
}
