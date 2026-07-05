import { PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Descriptions, Drawer, Form, Input, Modal, Select, Space, Table, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api, apiErrorMessage } from '../api/client';
import JsonBlock from '../components/JsonBlock';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import type { EmailIngestRequest, EmailItem, ParseResult } from '../types/api';
import { compactText, formatTime, numberText } from '../utils/format';

type EmailFilters = {
  keyword?: string;
  parse_status?: string;
  intent_type?: string;
};

export default function EmailsPage() {
  const [filters, setFilters] = useState<EmailFilters>({});
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [ingestOpen, setIngestOpen] = useState(false);
  const queryClient = useQueryClient();
  const handleMutationError = (error: unknown) => message.error(apiErrorMessage(error));
  const emailsQuery = useQuery({
    queryKey: ['emails', filters, page],
    queryFn: () => api.emails({ ...filters, page, page_size: 20 }),
  });
  const detailQuery = useQuery({
    queryKey: ['email-detail', selectedId],
    queryFn: () => api.emailDetail(selectedId as number),
    enabled: Boolean(selectedId),
  });
  const ingestMutation = useMutation({
    mutationFn: api.ingestEmail,
    onSuccess: () => {
      message.success('邮件已入库');
      setIngestOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['emails'] });
      void queryClient.invalidateQueries({ queryKey: ['tickets'] });
    },
    onError: handleMutationError,
  });
  const reparseMutation = useMutation({
    mutationFn: (id: number) => api.reparseEmail(id),
    onSuccess: () => {
      message.success('重解析已完成');
      void queryClient.invalidateQueries({ queryKey: ['email-detail', selectedId] });
      void queryClient.invalidateQueries({ queryKey: ['tickets'] });
    },
    onError: handleMutationError,
  });

  const columns: ColumnsType<EmailItem> = [
    { title: '主题', dataIndex: 'subject', ellipsis: true, render: (value?: string) => value || '-' },
    { title: '发件人', dataIndex: 'from_address', ellipsis: true, width: 220 },
    { title: '意图', dataIndex: 'intent_type', width: 130, render: (value?: string) => <StatusTag value={value} /> },
    { title: '解析', dataIndex: 'parse_status', width: 110, render: (value: string) => <StatusTag value={value} kind="parse" /> },
    { title: '线程', dataIndex: 'thread_id', width: 90, render: numberText },
    { title: '收信时间', dataIndex: 'received_at', width: 160, render: formatTime },
    {
      title: '操作',
      width: 130,
      render: (_, record) => (
        <Space>
          <Button type="link" size="small" onClick={() => setSelectedId(record.id)}>
            详情
          </Button>
          <Button
            type="link"
            size="small"
            icon={<ReloadOutlined />}
            onClick={() => Modal.confirm({
              title: '确认重新解析该邮件？',
              content: '该操作会生成新的解析候选，并可能更新关联工单。',
              okText: '确认',
              cancelText: '取消',
              onOk: () => reparseMutation.mutate(record.id),
            })}
          />
        </Space>
      ),
    },
  ];

  return (
    <div className="page-stack">
      <PageTitle title="邮件中心" extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => setIngestOpen(true)} />} />
      <SectionPanel>
        <Form<EmailFilters> layout="inline" className="filter-bar" onFinish={(values) => { setPage(1); setFilters(values); }}>
          <Form.Item name="keyword">
            <Input allowClear prefix={<SearchOutlined />} placeholder="主题、发件人、正文" />
          </Form.Item>
          <Form.Item name="intent_type">
            <Select
              allowClear
              placeholder="邮件意图"
              style={{ width: 150 }}
              options={[
                { value: 'new_repair', label: '新报修' },
                { value: 'customer_reply', label: '客户补充' },
                { value: 'internal_forward', label: '内部转发' },
                { value: 'irrelevant', label: '无关' },
                { value: 'unknown', label: '未知' },
              ]}
            />
          </Form.Item>
          <Form.Item name="parse_status">
            <Select
              allowClear
              placeholder="解析状态"
              style={{ width: 140 }}
              options={[
                { value: 'pending', label: '待解析' },
                { value: 'parsed', label: '已解析' },
                { value: 'skipped', label: '已跳过' },
              ]}
            />
          </Form.Item>
          <Button htmlType="submit" type="primary">筛选</Button>
        </Form>
        <Table<EmailItem>
          rowKey="id"
          columns={columns}
          dataSource={emailsQuery.data?.items ?? []}
          loading={emailsQuery.isFetching}
          locale={{ emptyText: emailsQuery.isError ? '邮件加载失败' : '暂无邮件' }}
          pagination={{ current: page, pageSize: 20, total: emailsQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
        />
      </SectionPanel>
      <Drawer width={760} title="邮件详情" open={Boolean(selectedId)} onClose={() => setSelectedId(null)}>
        {detailQuery.data ? (
          <div className="drawer-stack">
            <Descriptions column={1} size="small" bordered>
              <Descriptions.Item label="主题">{detailQuery.data.email.subject || '-'}</Descriptions.Item>
              <Descriptions.Item label="发件人">{detailQuery.data.email.from_address}</Descriptions.Item>
              <Descriptions.Item label="收件人">{detailQuery.data.email.to_addresses || '-'}</Descriptions.Item>
              <Descriptions.Item label="Message-ID">{detailQuery.data.email.message_id}</Descriptions.Item>
              <Descriptions.Item label="正文">{compactText(detailQuery.data.email.clean_body, '-')}</Descriptions.Item>
            </Descriptions>
            <div>
              <Typography.Title level={5}>解析候选</Typography.Title>
              <Table<ParseResult>
                size="small"
                rowKey="id"
                dataSource={detailQuery.data.parse_results}
                pagination={false}
                columns={[
                  { title: '解析器', dataIndex: 'parser_type', width: 90 },
                  { title: '意图', dataIndex: 'intent_type', width: 120 },
                  { title: '置信度', dataIndex: 'confidence_score', width: 90, render: numberText },
                  { title: '应用状态', dataIndex: 'apply_status', width: 120, render: (value: string) => <StatusTag value={value} /> },
                  { title: '缺失字段', dataIndex: 'missing_fields', render: (value) => <JsonBlock value={value} /> },
                ]}
              />
            </div>
          </div>
        ) : null}
      </Drawer>
      <Modal title="手动邮件入库" open={ingestOpen} onCancel={() => setIngestOpen(false)} footer={null} destroyOnClose>
        <Form<EmailIngestRequest>
          layout="vertical"
          initialValues={{ mailbox_account: 'manual', folder_name: 'INBOX' }}
          onFinish={(values) => ingestMutation.mutate(values)}
        >
          <Form.Item label="邮箱账号" name="mailbox_account" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item label="发件人" name="from_address" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item label="收件人" name="to_addresses">
            <Input />
          </Form.Item>
          <Form.Item label="主题" name="subject">
            <Input />
          </Form.Item>
          <Form.Item label="正文" name="text_body">
            <Input.TextArea rows={7} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={ingestMutation.isPending}>
            入库
          </Button>
        </Form>
      </Modal>
    </div>
  );
}
