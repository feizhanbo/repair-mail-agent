import {
  BellOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  MailOutlined,
  ProfileOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  TeamOutlined,
} from '@ant-design/icons';
import { Badge, Button, Layout, Menu, Space, Tag, Typography } from 'antd';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';

const { Header, Sider, Content } = Layout;

const menuItems = [
  { key: '/', icon: <DashboardOutlined />, label: '首页看板' },
  { key: '/emails', icon: <MailOutlined />, label: '邮件中心' },
  { key: '/tickets', icon: <ProfileOutlined />, label: '工单中心' },
  { key: '/manual-review', icon: <TeamOutlined />, label: '人工复核' },
  { key: '/replies', icon: <SafetyCertificateOutlined />, label: '自动回复审核' },
  { key: '/master-data', icon: <DatabaseOutlined />, label: '基础资料' },
  { key: '/ai-logs', icon: <RobotOutlined />, label: 'AI 日志' },
  { key: '/system', icon: <SettingOutlined />, label: '系统配置' },
];

export default function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();

  return (
    <Layout className="app-shell">
      <Sider width={232} theme="light" className="app-sider">
        <div className="brand-block">
          <Typography.Title level={4}>邮件报修自动化系统</Typography.Title>
          <Tag color="blue">一期试运行</Tag>
        </div>
        <Menu
          mode="inline"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={(item) => navigate(item.key)}
        />
      </Sider>
      <Layout>
        <Header className="app-header">
          <Typography.Text strong>repair-mail-agent</Typography.Text>
          <Space size="middle">
            <Tag color="green">dev</Tag>
            <Badge count={0} size="small">
              <Button aria-label="通知" icon={<BellOutlined />} shape="circle" />
            </Badge>
            <Typography.Text>未登录</Typography.Text>
          </Space>
        </Header>
        <Content className="app-content">
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}

