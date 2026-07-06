import {
  BellOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  LogoutOutlined,
  MailOutlined,
  MessageOutlined,
  ProfileOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Badge, Button, Layout, Menu, Space, Tag, Typography } from 'antd';
import { Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { useAuthStore } from '../stores/authStore';
import { hasAnyRole, hasRole } from '../utils/roles';

const { Header, Sider, Content } = Layout;

const baseMenuItems = [
  { key: '/', icon: <DashboardOutlined />, label: '首页看板' },
  { key: '/emails', icon: <MailOutlined />, label: '邮件中心' },
  { key: '/tickets', icon: <ProfileOutlined />, label: '工单中心' },
  { key: '/manual-review', icon: <TeamOutlined />, label: '人工复核' },
  { key: '/notifications', icon: <MessageOutlined />, label: '站内消息' },
];

function selectedMenuKey(pathname: string, items: Array<{ key: string }>) {
  const match = items.find((item) => item.key !== '/' && pathname.startsWith(item.key));
  return match?.key ?? '/';
}

export default function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { token, user, clearSession } = useAuthStore();
  const notificationsQuery = useQuery({
    queryKey: ['notifications', 'pending'],
    queryFn: () => api.notifications({ page: 1, page_size: 20, delivery_status: 'pending' }),
    enabled: Boolean(token),
    staleTime: 30_000,
  });

  if (!token) {
    return <Navigate to="/login" replace />;
  }

  const canAdmin = hasRole(user?.roles, 'admin');
  const canSupervise = hasAnyRole(user?.roles, ['admin', 'supervisor']);
  const menuItems = [
    ...baseMenuItems.slice(0, 4),
    ...(canSupervise ? [{ key: '/replies', icon: <SafetyCertificateOutlined />, label: '自动回复审核' }] : []),
    ...(canSupervise ? [{ key: '/master-data', icon: <DatabaseOutlined />, label: '基础资料' }] : []),
    ...(canAdmin ? [{ key: '/users', icon: <UserOutlined />, label: '用户管理' }] : []),
    ...(canAdmin ? [{ key: '/db-browser', icon: <DatabaseOutlined />, label: '数据库浏览' }] : []),
    ...baseMenuItems.slice(4),
    ...(canSupervise ? [{ key: '/ai-logs', icon: <RobotOutlined />, label: 'AI 日志' }] : []),
    ...(canSupervise ? [{ key: '/system', icon: <SettingOutlined />, label: '系统配置' }] : []),
  ];

  return (
    <Layout className="app-shell">
      <Sider width={232} theme="light" className="app-sider">
        <div className="brand-block">
          <Typography.Title level={4}>邮件报修自动化系统</Typography.Title>
          <Tag color="blue">一期试运行</Tag>
        </div>
        <Menu
          mode="inline"
          selectedKeys={[selectedMenuKey(location.pathname, menuItems)]}
          items={menuItems}
          onClick={(item) => navigate(item.key)}
        />
      </Sider>
      <Layout>
        <Header className="app-header">
          <Typography.Text strong>repair-mail-agent</Typography.Text>
          <Space size="middle">
            <Tag color="green">dev</Tag>
            <Badge count={notificationsQuery.data?.total ?? 0} size="small">
              <Button aria-label="通知" icon={<BellOutlined />} shape="circle" onClick={() => navigate('/notifications')} />
            </Badge>
            <Button type="text" onClick={() => navigate('/profile')}>{user?.real_name ?? user?.username}</Button>
            <Button aria-label="退出" icon={<LogoutOutlined />} shape="circle" onClick={clearSession} />
          </Space>
        </Header>
        <Content className="app-content">
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
