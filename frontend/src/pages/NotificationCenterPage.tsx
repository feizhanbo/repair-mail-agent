import {
  AlertOutlined,
  CheckCircleOutlined,
  FileTextOutlined,
  TeamOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Badge, Button, Empty, List, Space, Tabs, Tag, Typography, message } from 'antd';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, apiErrorMessage } from '../api/client';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import type { NotificationCenterItem } from '../types/api';
import { formatTime } from '../utils/format';
import { useAuthStore } from '../stores/authStore';

function eventIcon(eventType: string) {
  if (eventType.includes('manual_review')) return <TeamOutlined />;
  if (eventType.includes('error') || eventType.includes('failed') || eventType.includes('uncertain')) {
    return <AlertOutlined />;
  }
  if (eventType.includes('customer_info')) return <FileTextOutlined />;
  return <CheckCircleOutlined />;
}

function targetPath(record: NotificationCenterItem) {
  if (record.target_type === 'manual_review_task') return `/manual-review?task_id=${record.target_id}`;
  if (record.ticket_id || record.target_type === 'repair_ticket' || record.target_type === 'ticket') {
    return `/tickets?ticket_id=${record.ticket_id ?? record.target_id}`;
  }
  return '/notifications';
}

export default function NotificationCenterPage() {
  const currentUserId = useAuthStore((state) => state.user?.id);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<'all' | 'unread'>('all');
  const unreadOnly = activeTab === 'unread';
  const query = useQuery({
    queryKey: ['notification-center', unreadOnly],
    queryFn: () => api.notificationCenter({ page: 1, page_size: 50, unread_only: unreadOnly }),
  });
  const readGroupMutation = useMutation({
    mutationFn: (ticketId: number) => api.markNotificationCenterGroupRead(ticketId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['notification-center'] });
      void queryClient.invalidateQueries({ queryKey: ['notification-center-summary'] });
      void queryClient.invalidateQueries({ queryKey: ['notifications-page'] });
    },
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const list = query.data?.items ?? [];
  const unread = query.data?.unread_total ?? list.filter((item) => item.status === 'unread').length;

  const jumpToTarget = async (item: NotificationCenterItem) => {
    try {
      if (item.ticket_id && item.state_user_id === currentUserId) {
        await api.markNotificationCenterGroupRead(item.ticket_id);
      }
      void queryClient.invalidateQueries({ queryKey: ['notification-center'] });
      void queryClient.invalidateQueries({ queryKey: ['notification-center-summary'] });
      void queryClient.invalidateQueries({ queryKey: ['notifications-page'] });
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
          <Space>
            <Badge count={unread} size="small">
              <Button onClick={() => navigate('/notifications')}>查看站内消息</Button>
            </Badge>
          </Space>
        )}
      />
      <SectionPanel className="notification-center-panel">
        <Typography.Paragraph type="secondary">
          这里仅展示仍需关注的报修问题；标记已读不会关闭问题，问题解决后会自动从本列表移除。
        </Typography.Paragraph>
        <Tabs
          activeKey={activeTab}
          onChange={(value) => setActiveTab(value as 'all' | 'unread')}
          items={[
            { key: 'all', label: `待关注 (${query.data?.total ?? 0})` },
            { key: 'unread', label: `未读 (${unread})` },
          ]}
        />
        {list.length ? (
          <List
            loading={query.isFetching}
            dataSource={list}
            renderItem={(item) => (
              <List.Item
                className={item.status === 'read' ? 'notification-row is-read' : 'notification-row'}
                actions={[
                  <Button key="detail" type="link" onClick={() => void jumpToTarget(item)}>查看处理</Button>,
                  <Button
                    key="read"
                    type="link"
                    disabled={item.status === 'read' || !item.ticket_id || item.state_user_id !== currentUserId}
                    onClick={() => item.ticket_id && readGroupMutation.mutate(item.ticket_id)}
                  >
                    标记已读
                  </Button>,
                ]}
              >
                <List.Item.Meta
                  avatar={<span className="notification-icon">{eventIcon(item.event_type)}</span>}
                  title={(
                    <Space wrap>
                      <Typography.Text strong>{item.title}</Typography.Text>
                      <Tag color={item.priority === 'high' ? 'red' : item.priority === 'normal' ? 'orange' : 'blue'}>
                        {item.priority}
                      </Tag>
                      {item.ticket_no ? <Tag color="geekblue">{item.ticket_no}</Tag> : null}
                      <Tag color="purple">{item.state_user_real_name || item.state_username || `用户 #${item.state_user_id}`}</Tag>
                      <Tag>{item.active_event_count} 个当前问题</Tag>
                      {item.unread_event_count ? <Tag color="green">{item.unread_event_count} 条未读</Tag> : null}
                    </Space>
                  )}
                  description={(
                    <div className="notification-description">
                      <Typography.Paragraph ellipsis={{ rows: 2 }}>{item.content || '-'}</Typography.Paragraph>
                      <Typography.Text type="secondary">最新更新：{formatTime(item.latest_created_at)}</Typography.Text>
                    </div>
                  )}
                />
              </List.Item>
            )}
          />
        ) : (
          <Empty description={query.isFetching ? '加载中' : '当前没有待关注的报修问题'} />
        )}
      </SectionPanel>
    </div>
  );
}
