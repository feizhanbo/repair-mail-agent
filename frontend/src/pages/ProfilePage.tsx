import { useMutation, useQuery } from '@tanstack/react-query';
import { Button, Descriptions, Form, Input, Space, Tag, message } from 'antd';
import { useEffect } from 'react';
import { api, apiErrorMessage } from '../api/client';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import type { RoleCode } from '../types/api';
import { roleLabels } from '../utils/roles';

type ProfileForm = {
  real_name?: string;
  email?: string;
  phone?: string;
};

type PasswordForm = {
  old_password: string;
  new_password: string;
};

export default function ProfilePage() {
  const [profileForm] = Form.useForm<ProfileForm>();
  const meQuery = useQuery({ queryKey: ['me'], queryFn: api.me });
  const handleError = (error: unknown) => message.error(apiErrorMessage(error));
  const profileMutation = useMutation({
    mutationFn: (values: ProfileForm) => api.updateProfile(values),
    onSuccess: () => {
      message.success('个人信息已更新');
      void meQuery.refetch();
    },
    onError: handleError,
  });
  const passwordMutation = useMutation({
    mutationFn: (values: PasswordForm) => api.changePassword(values),
    onSuccess: () => message.success('密码已修改'),
    onError: handleError,
  });
  const user = meQuery.data?.user;
  const roles = (meQuery.data?.roles ?? []) as RoleCode[];

  useEffect(() => {
    if (user) {
      profileForm.setFieldsValue({
        real_name: user.real_name,
        email: user.email ?? undefined,
        phone: user.phone ?? undefined,
      });
    }
  }, [profileForm, user]);

  return (
    <div className="page-stack">
      <PageTitle title="个人信息" />
      <SectionPanel>
        <Descriptions bordered column={2} size="small">
          <Descriptions.Item label="账号">{user?.username || '-'}</Descriptions.Item>
          <Descriptions.Item label="姓名">{user?.real_name || '-'}</Descriptions.Item>
          <Descriptions.Item label="邮箱">{user?.email || '-'}</Descriptions.Item>
          <Descriptions.Item label="电话">{user?.phone || '-'}</Descriptions.Item>
          <Descriptions.Item label="状态">{user?.status || '-'}</Descriptions.Item>
          <Descriptions.Item label="角色" span={2}>
            <Space wrap>{roles.map((role) => <Tag key={role} color="blue">{roleLabels[role] ?? role}</Tag>)}</Space>
          </Descriptions.Item>
        </Descriptions>
      </SectionPanel>
      <div className="two-column-grid">
        <SectionPanel>
          <h4>编辑资料</h4>
          <Form<ProfileForm>
            form={profileForm}
            layout="vertical"
            onFinish={(values) => profileMutation.mutate(values)}
          >
            <Form.Item label="姓名" name="real_name"><Input /></Form.Item>
            <Form.Item label="邮箱" name="email"><Input /></Form.Item>
            <Form.Item label="电话" name="phone"><Input /></Form.Item>
            <Button type="primary" htmlType="submit" loading={profileMutation.isPending}>保存资料</Button>
          </Form>
        </SectionPanel>
        <SectionPanel>
          <h4>修改密码</h4>
          <Form<PasswordForm> layout="vertical" onFinish={(values) => passwordMutation.mutate(values)}>
            <Form.Item label="原密码" name="old_password" rules={[{ required: true }]}><Input.Password /></Form.Item>
            <Form.Item label="新密码" name="new_password" rules={[{ required: true }]}><Input.Password /></Form.Item>
            <Button type="primary" htmlType="submit" loading={passwordMutation.isPending}>修改密码</Button>
          </Form>
        </SectionPanel>
      </div>
    </div>
  );
}
