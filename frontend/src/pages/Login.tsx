import { LockOutlined, UserOutlined } from '@ant-design/icons';
import { useMutation } from '@tanstack/react-query';
import { Button, Form, Input, Typography, message } from 'antd';
import { Navigate, useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { useAuthStore } from '../stores/authStore';
import type { LoginRequest } from '../types/api';

export default function Login() {
  const navigate = useNavigate();
  const { token, setSession } = useAuthStore();
  const mutation = useMutation({
    mutationFn: api.login,
    onSuccess: (data) => {
      setSession(data.access_token, data.user);
      navigate('/', { replace: true });
    },
    onError: () => message.error('用户名或密码不正确'),
  });

  if (token) {
    return <Navigate to="/" replace />;
  }

  return (
    <main className="login-page">
      <section className="login-panel">
        <Typography.Title level={3}>邮件报修自动化系统</Typography.Title>
        <Form<LoginRequest> layout="vertical" onFinish={(values) => mutation.mutate(values)}>
          <Form.Item label="用户名" name="username" rules={[{ required: true, message: '请输入用户名' }]}>
            <Input prefix={<UserOutlined />} autoComplete="username" />
          </Form.Item>
          <Form.Item label="密码" name="password" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password prefix={<LockOutlined />} autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={mutation.isPending}>
            登录
          </Button>
        </Form>
      </section>
    </main>
  );
}
