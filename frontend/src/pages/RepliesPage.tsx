import { PlusOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Descriptions, Drawer, Form, Input, Modal, Select, Space, Table, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, apiErrorMessage } from '../api/client';
import { waitForJob } from '../utils/jobs';
import ErrorResult from '../components/ErrorResult';
import JsonBlock from '../components/JsonBlock';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import { useAuthStore } from '../stores/authStore';
import type { ReplyRecord } from '../types/api';
import { compactText, formatTime } from '../utils/format';
import { hasAnyRole } from '../utils/roles';

type ReplyFilters = {
  review_status?: string;
  send_status?: string;
  ticket_id?: number;
};

type DraftForm = {
  ticket_id: number;
  reply_type?: string;
  language?: string;
};

export default function RepliesPage() {
  const [searchParams] = useSearchParams();
  const [filters, setFilters] = useState<ReplyFilters>({});
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<ReplyRecord | null>(null);
  const [draftOpen, setDraftOpen] = useState(false);
  const queryClient = useQueryClient();
  const currentRoles = useAuthStore((state) => state.user?.roles);
  const canDraftReplies = hasAnyRole(currentRoles, ['admin', 'supervisor', 'operator']);
  const canReviewReplies = hasAnyRole(currentRoles, ['admin', 'supervisor', 'operator']);
  const handleMutationError = (error: unknown) => message.error(apiErrorMessage(error));
  const confirmAction = (title: string, onOk: () => void) => {
    Modal.confirm({
      title,
      content: '该操作会更新回复审核状态。',
      okText: '确认',
      cancelText: '取消',
      onOk,
    });
  };
  const repliesQuery = useQuery({
    queryKey: ['replies', filters, page],
    queryFn: () => api.replies({ ...filters, page, page_size: 20 }),
  });
  useEffect(() => {
    const replyId = Number(searchParams.get('reply_id'));
    if (!selected && Number.isInteger(replyId) && replyId > 0) {
      const match = repliesQuery.data?.items.find((item) => item.id === replyId);
      if (match) setSelected(match);
    }
  }, [repliesQuery.data?.items, searchParams, selected]);
  const draftMutation = useMutation({
    mutationFn: (values: DraftForm) => api.draftReply(values.ticket_id, { reply_type: values.reply_type, language: values.language ?? 'zh-CN' }),
    onSuccess: () => {
      message.success('草稿已生成');
      setDraftOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['replies'] });
      void queryClient.invalidateQueries({ queryKey: ['manual-tasks'] });
    },
    onError: handleMutationError,
  });
  const approveMutation = useMutation({
    mutationFn: async (id: number) => {
      if (import.meta.env.VITE_SMTP_ASYNC_ENABLED !== 'true') return api.approveReply(id);
      const result = await api.approveReplyJob(id);
      return result.job ? waitForJob(result.job) : result;
    },
    onSuccess: () => {
      message.success('回复已审核');
      setSelected(null);
      void queryClient.invalidateQueries({ queryKey: ['replies'] });
    },
    onError: handleMutationError,
  });
  const rejectMutation = useMutation({
    mutationFn: ({ id, reason }: { id: number; reason: string }) => api.rejectReply(id, reason),
    onSuccess: () => {
      message.success('回复已驳回');
      setSelected(null);
      void queryClient.invalidateQueries({ queryKey: ['replies'] });
    },
    onError: handleMutationError,
  });
  const reconcileMutation = useMutation({
    mutationFn: ({ id, outcome }: { id: number; outcome: 'sent' | 'failed' }) => api.reconcileReplySend(id, {
      outcome,
      reason: outcome === 'sent' ? '操作员核对测试邮箱后确认邮件实际已发送。' : '操作员核对测试邮箱后确认邮件未发送。',
    }),
    onSuccess: () => {
      message.success('不确定发送结果已核对');
      setSelected(null);
      void queryClient.invalidateQueries({ queryKey: ['replies'] });
      void queryClient.invalidateQueries({ queryKey: ['tickets'] });
    },
    onError: handleMutationError,
  });

  const columns: ColumnsType<ReplyRecord> = [
    { title: '工单', dataIndex: 'ticket_id', width: 90 },
    { title: '类型', dataIndex: 'reply_type', width: 150 },
    { title: '主题', dataIndex: 'subject', ellipsis: true, render: (value?: string) => value || '-' },
    { title: '审核', dataIndex: 'review_status', width: 110, render: (value: string) => <StatusTag value={value} kind="review" /> },
    { title: '发送', dataIndex: 'send_status', width: 120, render: (value: string) => <StatusTag value={value} /> },
    { title: '轮次', dataIndex: 'followup_round', width: 80 },
    { title: '创建时间', dataIndex: 'created_at', width: 160, render: formatTime },
    { title: '操作', width: 90, render: (_, record) => <Button type="link" size="small" onClick={() => setSelected(record)}>{canReviewReplies ? '审核' : '详情'}</Button> },
  ];

  return (
    <div className="page-stack">
      <PageTitle title="回复管理" extra={canDraftReplies ? <Button type="primary" icon={<PlusOutlined />} onClick={() => setDraftOpen(true)} /> : null} />
      <SectionPanel>
        <Form<ReplyFilters> layout="inline" className="filter-bar" onFinish={(values) => { setPage(1); setFilters(values); }}>
          <Form.Item name="ticket_id">
            <Input type="number" placeholder="工单 ID" />
          </Form.Item>
          <Form.Item name="review_status">
            <Select
              allowClear
              placeholder="审核状态"
              style={{ width: 140 }}
              options={[
                { value: 'pending', label: '待审核' },
                { value: 'approved', label: '已通过' },
                { value: 'rejected', label: '已驳回' },
              ]}
            />
          </Form.Item>
          <Button htmlType="submit" type="primary">筛选</Button>
        </Form>
        <Table<ReplyRecord>
          rowKey="id"
          columns={columns}
          dataSource={repliesQuery.data?.items ?? []}
          loading={repliesQuery.isFetching}
          locale={{
            emptyText: repliesQuery.isError
              ? <ErrorResult message={apiErrorMessage(repliesQuery.error)} onRetry={() => repliesQuery.refetch()} />
              : '暂无回复记录'
          }}
          pagination={{ current: page, pageSize: 20, total: repliesQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
        />
      </SectionPanel>
      <Drawer
        width={760}
        title="回复审核"
        open={Boolean(selected)}
        onClose={() => setSelected(null)}
        extra={
          selected && canReviewReplies ? (
            <Space>
              {selected.send_status === 'send_uncertain' ? (
                <>
                  <Button danger onClick={() => confirmAction('确认该邮件实际未发送？', () => reconcileMutation.mutate({ id: selected.id, outcome: 'failed' }))}>确认未发送</Button>
                  <Button type="primary" onClick={() => confirmAction('确认该邮件实际已发送？', () => reconcileMutation.mutate({ id: selected.id, outcome: 'sent' }))}>确认已发送</Button>
                </>
              ) : (
                <>
                  <Button danger onClick={() => confirmAction('确认驳回该回复？', () => rejectMutation.mutate({ id: selected.id, reason: '人工驳回' }))}>驳回</Button>
                  <Button type="primary" onClick={() => confirmAction('确认审核通过该回复？', () => approveMutation.mutate(selected.id))}>通过</Button>
                </>
              )}
            </Space>
          ) : null
        }
      >
        {selected ? (
          <div className="drawer-stack">
            <Descriptions column={1} size="small" bordered>
              <Descriptions.Item label="收件人">{selected.to_addresses}</Descriptions.Item>
              <Descriptions.Item label="主题">{selected.subject || '-'}</Descriptions.Item>
              <Descriptions.Item label="正文">{selected.final_body || selected.draft_body || '-'}</Descriptions.Item>
              <Descriptions.Item label="发送状态">{selected.error_message || selected.send_status}</Descriptions.Item>
              <Descriptions.Item label="缺失字段"><JsonBlock value={selected.missing_fields} /></Descriptions.Item>
            </Descriptions>
          </div>
        ) : null}
      </Drawer>
      <Modal title="生成回复草稿" open={draftOpen} onCancel={() => setDraftOpen(false)} footer={null} destroyOnClose>
        <Form<DraftForm> layout="vertical" initialValues={{ language: 'zh-CN' }} onFinish={(values) => draftMutation.mutate(values)}>
          <Form.Item label="工单 ID" name="ticket_id" rules={[{ required: true }]}>
            <Input type="number" />
          </Form.Item>
          <Form.Item label="回复类型" name="reply_type">
            <Select
              allowClear
              options={[
                { value: 'missing_fields', label: '缺失字段追问' },
                { value: 'sn_invalid', label: 'SN 确认' },
                { value: 'manual_review', label: '人工审核提醒' },
              ]}
            />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={draftMutation.isPending}>
            生成
          </Button>
        </Form>
      </Modal>
    </div>
  );
}
