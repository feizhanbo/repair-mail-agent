import { DeleteOutlined, EditOutlined, KeyOutlined, PlusOutlined, StopOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Drawer, Form, Input, Modal, Select, Space, Table, Tag, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api, apiErrorMessage } from '../api/client';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import type { RoleCode, UserAccount, UserCreateRequest } from '../types/api';
import { formatTime } from '../utils/format';
import { roleLabels, roleOptions } from '../utils/roles';

type UserForm = UserCreateRequest;

export default function UsersPage() {
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState<string>();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<UserAccount | null>(null);
  const [form] = Form.useForm<UserForm>();
  const queryClient = useQueryClient();
  const usersQuery = useQuery({
    queryKey: ['users', page, keyword],
    queryFn: () => api.users({ page, page_size: 20, keyword }),
  });
  const refreshUsers = () => void queryClient.invalidateQueries({ queryKey: ['users'] });
  const handleError = (error: unknown) => message.error(apiErrorMessage(error));

  const saveMutation = useMutation({
    mutationFn: (values: UserForm) => {
      if (editing) {
        const { roles, status, username: _username, password: _password, ...profileValues } = values;
        return api
          .updateUser(editing.id, profileValues)
          .then(() => api.updateUserRoles(editing.id, roles ?? []))
          .then(() => (status !== editing.status ? api.updateUserStatus(editing.id, status) : Promise.resolve(editing)));
      }
      return api.createUser(values);
    },
    onSuccess: () => {
      message.success(editing ? '用户已更新' : '用户已创建');
      setDrawerOpen(false);
      setEditing(null);
      form.resetFields();
      refreshUsers();
    },
    onError: handleError,
  });
  const statusMutation = useMutation({
    mutationFn: (user: UserAccount) => api.updateUserStatus(user.id, user.status === 'active' ? 'disabled' : 'active'),
    onSuccess: () => {
      message.success('用户状态已更新');
      refreshUsers();
    },
    onError: handleError,
  });
  const resetPasswordMutation = useMutation({
    mutationFn: ({ id, password }: { id: number; password: string }) => api.resetUserPassword(id, password),
    onSuccess: () => {
      message.success('密码已重置');
      refreshUsers();
    },
    onError: handleError,
  });
  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteUser(id),
    onSuccess: () => {
      message.success('用户已删除');
      refreshUsers();
    },
    onError: handleError,
  });

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ status: 'active', roles: ['operator'] as RoleCode[] });
    setDrawerOpen(true);
  };
  const openEdit = (record: UserAccount) => {
    setEditing(record);
    form.setFieldsValue({
      username: record.username,
      password: '',
      real_name: record.real_name,
      email: record.email ?? undefined,
      phone: record.phone ?? undefined,
      department: record.department ?? undefined,
      status: record.status as 'active' | 'disabled',
      roles: record.roles,
    });
    setDrawerOpen(true);
  };

  const columns: ColumnsType<UserAccount> = [
    { title: '账号', dataIndex: 'username', width: 150 },
    { title: '姓名', dataIndex: 'real_name', width: 140 },
    { title: '部门', dataIndex: 'department', width: 140, render: (value?: string) => value || '-' },
    { title: '邮箱', dataIndex: 'email', ellipsis: true, render: (value?: string) => value || '-' },
    {
      title: '角色',
      dataIndex: 'roles',
      render: (roles: RoleCode[]) => (
        <Space wrap>{roles.map((role) => <Tag key={role} color="blue">{roleLabels[role] ?? role}</Tag>)}</Space>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 90, render: (value: string) => <Tag color={value === 'active' ? 'green' : 'default'}>{value === 'active' ? '启用' : '禁用'}</Tag> },
    { title: '最后登录', dataIndex: 'last_login_at', width: 160, render: formatTime },
    {
      title: '操作',
      width: 290,
      render: (_, record) => (
        <Space>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(record)}>编辑</Button>
          <Button
            size="small"
            icon={<StopOutlined />}
            onClick={() => Modal.confirm({ title: '确认更新用户状态？', onOk: () => statusMutation.mutate(record) })}
          >
            {record.status === 'active' ? '禁用' : '启用'}
          </Button>
          <Button
            size="small"
            icon={<KeyOutlined />}
            onClick={() => {
              let password = '';
              Modal.confirm({
                title: '重置密码',
                content: <Input.Password placeholder="输入新密码" onChange={(event) => { password = event.target.value; }} />,
                onOk: () => {
                  const nextPassword = password.trim();
                  if (!nextPassword) {
                    message.error('请输入新密码');
                    return Promise.reject(new Error('password required'));
                  }
                  return resetPasswordMutation.mutateAsync({ id: record.id, password: nextPassword });
                },
              });
            }}
          >
            重置
          </Button>
          <Button
            size="small"
            danger
            icon={<DeleteOutlined />}
            loading={deleteMutation.isPending}
            onClick={() => Modal.confirm({
              title: '确认删除该用户？',
              content: `将物理删除用户 ${record.real_name}（${record.username}）。已有业务引用时后端会拒绝删除。`,
              okText: '删除',
              okButtonProps: { danger: true },
              onOk: () => deleteMutation.mutateAsync(record.id),
            })}
          >
            删除
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div className="page-stack">
      <PageTitle
        title="用户管理"
        extra={<Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增用户</Button>}
      />
      <SectionPanel>
        <Space className="filter-bar">
          <Input.Search
            allowClear
            placeholder="账号、姓名、邮箱、部门"
            onSearch={(value) => {
              setKeyword(value || undefined);
              setPage(1);
            }}
            style={{ width: 280 }}
          />
        </Space>
        <Table<UserAccount>
          rowKey="id"
          columns={columns}
          dataSource={usersQuery.data?.items ?? []}
          loading={usersQuery.isFetching}
          pagination={{ current: page, pageSize: 20, total: usersQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
        />
      </SectionPanel>
      <Drawer
        title={editing ? '编辑用户' : '新增用户'}
        width={520}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        destroyOnClose
      >
        <Form<UserForm> form={form} layout="vertical" onFinish={(values) => saveMutation.mutate(values)}>
          <Form.Item label="账号" name="username" rules={[{ required: true }]}>
            <Input disabled={Boolean(editing)} />
          </Form.Item>
          {!editing ? (
            <Form.Item label="初始密码" name="password" rules={[{ required: true }]}>
              <Input.Password />
            </Form.Item>
          ) : null}
          <Form.Item label="姓名" name="real_name" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item label="邮箱" name="email">
            <Input />
          </Form.Item>
          <Form.Item label="电话" name="phone">
            <Input />
          </Form.Item>
          <Form.Item label="部门" name="department">
            <Input />
          </Form.Item>
          <Form.Item label="状态" name="status" rules={[{ required: true }]}>
            <Select options={[{ value: 'active', label: '启用' }, { value: 'disabled', label: '禁用' }]} />
          </Form.Item>
          <Form.Item label="角色" name="roles" rules={[{ required: true }]}>
            <Select mode="multiple" options={roleOptions} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={saveMutation.isPending}>保存</Button>
        </Form>
      </Drawer>
    </div>
  );
}
