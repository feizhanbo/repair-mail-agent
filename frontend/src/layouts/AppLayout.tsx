import {
  BellOutlined,
  BarChartOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  LogoutOutlined,
  MailOutlined,
  MessageOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  ProfileOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Badge, Button, Layout, Menu, Space, Typography } from 'antd';
import { useState } from 'react';
import { Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { ActiveJobMonitor } from '../components/ActiveJobMonitor';
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
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('repair_mail_sidebar_collapsed') === 'true');
  const notificationsQuery = useQuery({
    queryKey: ['notification-center-summary'],
    queryFn: api.notificationCenterSummary,
    enabled: Boolean(token),
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

  if (!token) {
    return <Navigate to="/login" replace />;
  }

  const canAdmin = hasRole(user?.roles, 'admin');
  const canOperate = hasAnyRole(user?.roles, ['admin', 'operator']);
  const toggleCollapsed = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem('repair_mail_sidebar_collapsed', String(next));
  };
  const menuItems = [
    ...baseMenuItems.slice(0, 4),
    { key: '/replies', icon: <SafetyCertificateOutlined />, label: '回复管理' },
    ...(canOperate ? [{ key: '/statistics', icon: <BarChartOutlined />, label: '统计分析' }] : []),
    ...(canOperate ? [{ key: '/master-data', icon: <DatabaseOutlined />, label: '基础资料' }] : []),
    ...(canAdmin ? [{ key: '/users', icon: <UserOutlined />, label: '用户管理' }] : []),
    ...(canAdmin ? [{ key: '/db-browser', icon: <DatabaseOutlined />, label: '数据库浏览' }] : []),
    ...baseMenuItems.slice(4),
    ...(canOperate ? [{ key: '/ai-logs', icon: <RobotOutlined />, label: 'AI 日志' }] : []),
    ...(canOperate ? [{ key: '/system', icon: <SettingOutlined />, label: '系统配置' }] : []),
  ];

  return (
    <Layout className="app-shell">
      <ActiveJobMonitor />
      <Sider width={232} theme="light" className="app-sider" collapsible collapsed={collapsed} trigger={null}>
        <div className="brand-block">
          {collapsed ? (
            <Typography.Text strong className="brand-mark">修</Typography.Text>
          ) : (
            <Typography.Title level={4}>邮件报修系统</Typography.Title>
          )}
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
          <Button
            aria-label="折叠侧边栏"
            icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            type="text"
            onClick={toggleCollapsed}
          />
          <Space size="middle">
            <Badge count={notificationsQuery.data?.attention_count ?? 0} size="small">
              <Button aria-label="通知中心" icon={<BellOutlined />} shape="circle" onClick={() => navigate('/notification-center')} />
            </Badge>
            <Button type="text" onClick={() => navigate('/profile')}>{user?.real_name ?? user?.username}</Button>
            <Button
              aria-label="退出"
              icon={<LogoutOutlined />}
              shape="circle"
              onClick={() => { void api.logout().finally(clearSession); }}
            />
          </Space>
        </Header>
        <Content className="app-content">
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
