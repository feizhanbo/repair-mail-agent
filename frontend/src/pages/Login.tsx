import { LockOutlined, UserOutlined } from '@ant-design/icons';
import { Button, Form, Input, Typography } from 'antd';

export default function Login() {
  return (
    <main className="login-page">
      <section className="login-panel">
        <Typography.Title level={3}>邮件报修自动化系统</Typography.Title>
        <Form layout="vertical">
          <Form.Item label="用户名" name="username">
            <Input prefix={<UserOutlined />} autoComplete="username" />
          </Form.Item>
          <Form.Item label="密码" name="password">
            <Input.Password prefix={<LockOutlined />} autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block>
            登录
          </Button>
        </Form>
      </section>
    </main>
  );
}

