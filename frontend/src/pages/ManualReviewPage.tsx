import {
  CheckCircleOutlined,
  EditOutlined,
  FileAddOutlined,
  MailOutlined,
  SearchOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Button,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Timeline,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useEffect, useState } from 'react';
import { api, apiErrorMessage } from '../api/client';
import JsonBlock from '../components/JsonBlock';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import { useAuthStore } from '../stores/authStore';
import type {
  Attachment,
  EmailItem,
  FieldAuditLog,
  ManualTask,
  ManualTaskDetail,
  ParseResult,
  ReplyRecord,
  TicketLine,
  UserAccount,
} from '../types/api';
import { compactText, formatTime, numberText } from '../utils/format';
import { hasAnyRole } from '../utils/roles';

type TaskFilters = {
  status?: string;
  task_type?: string;
  scope?: string;
};

type ResolveForm = {
  resolution: string;
  resolution_type?: string;
  next_action: string;
  result_payload_text?: string;
};

type AssignForm = {
  assigned_user_id?: number;
  reason?: string;
};

type TicketFieldForm = {
  customer_code?: string;
  customer_name?: string;
  contact_person?: string;
  contact_phone?: string;
  contact_email?: string;
  request_date?: string;
  mailing_address?: string;
  problem_description?: string;
  accessories?: string;
};

type TicketItemForm = {
  line_no?: number;
  material_code?: string;
  material_name?: string;
  sn?: string;
  quantity?: number;
  failure_description?: string;
  failure_information?: string;
  data_info?: string;
  remarks?: string;
  accessories?: string;
  manual_locked?: boolean;
};

const taskStatusOptions = [
  { value: 'pending', label: '待领取' },
  { value: 'assigned', label: '已分配' },
  { value: 'claimed', label: '处理中' },
  { value: 'resolved', label: '已解决' },
];

const resolveActionOptions = [
  { value: 'transition_ready_for_export', label: '进入可导出' },
  { value: 'generate_followup', label: '生成追问' },
  { value: 'wait_customer_info', label: '等待客户补充' },
  { value: 'reparse', label: '重新解析' },
  { value: 'close_ticket', label: '关闭工单' },
  { value: 'keep_manual_review', label: '保持人工复核' },
];

const taskScopeOptions = [
  { value: 'mine', label: '我的任务' },
  { value: 'unassigned', label: '未分配' },
  { value: 'claimed', label: '我已领取' },
  { value: 'all', label: '全部任务' },
];

const resolutionTypeOptions = [
  { value: 'field_fixed', label: '字段已修正' },
  { value: 'sn_checked', label: 'SN 已核验' },
  { value: 'need_followup', label: '需要追问' },
  { value: 'false_positive', label: '无需处理' },
  { value: 'system_exception', label: '系统异常' },
  { value: 'other', label: '其他' },
];

const lockOptions = [
  { value: false, label: '未锁定' },
  { value: true, label: '人工锁定' },
];

export default function ManualReviewPage() {
  const [filters, setFilters] = useState<TaskFilters>({});
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [resolveOpen, setResolveOpen] = useState(false);
  const [assignOpen, setAssignOpen] = useState(false);
  const [fieldOpen, setFieldOpen] = useState(false);
  const [itemOpen, setItemOpen] = useState(false);
  const [editingItem, setEditingItem] = useState<TicketLine | null>(null);
  const queryClient = useQueryClient();
  const currentUserRoles = useAuthStore((state) => state.user?.roles);
  const canAssignTasks = hasAnyRole(currentUserRoles, ['admin', 'supervisor']);
  const visibleTaskScopeOptions = canAssignTasks ? taskScopeOptions : taskScopeOptions.filter((item) => item.value !== 'all');
  const handleMutationError = (error: unknown) => message.error(apiErrorMessage(error));
  const confirmAction = (title: string, onOk: () => void) => {
    Modal.confirm({
      title,
      content: '该操作会更新当前复核任务或关联工单。',
      okText: '确认',
      cancelText: '取消',
      onOk,
    });
  };

  const tasksQuery = useQuery({
    queryKey: ['manual-tasks', filters, page],
    queryFn: () => api.manualTasks({ ...filters, page, page_size: 20 }),
  });
  const detailQuery = useQuery({
    queryKey: ['manual-task-detail', selectedId],
    queryFn: () => api.manualTaskDetail(selectedId as number),
    enabled: Boolean(selectedId),
  });
  const usersQuery = useQuery({
    queryKey: ['users', 'active-assignees'],
    queryFn: () => api.users({ page: 1, page_size: 100, status: 'active' }),
    enabled: canAssignTasks,
  });

  useEffect(() => {
    if (!selectedId && tasksQuery.data?.items.length) {
      setSelectedId(tasksQuery.data.items[0].id);
    }
  }, [selectedId, tasksQuery.data?.items]);

  const invalidateWorkbench = () => {
    void queryClient.invalidateQueries({ queryKey: ['manual-tasks'] });
    void queryClient.invalidateQueries({ queryKey: ['manual-task-detail', selectedId] });
    void queryClient.invalidateQueries({ queryKey: ['tickets'] });
    void queryClient.invalidateQueries({ queryKey: ['ticket-detail'] });
    void queryClient.invalidateQueries({ queryKey: ['notifications'] });
  };

  const claimMutation = useMutation({
    mutationFn: (id: number) => api.claimTask(id),
    onSuccess: () => {
      message.success('任务已领取');
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const releaseMutation = useMutation({
    mutationFn: (id: number) => api.releaseTask(id),
    onSuccess: () => {
      message.success('任务已释放');
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const resolveMutation = useMutation({
    mutationFn: (values: ResolveForm) => {
      let result_payload: Record<string, unknown> | undefined;
      if (values.result_payload_text?.trim()) {
        try {
          result_payload = JSON.parse(values.result_payload_text);
        } catch {
          result_payload = { note: values.result_payload_text };
        }
      }
      return api.resolveTask(selectedId as number, {
        resolution: values.resolution,
        resolution_type: values.resolution_type,
        next_action: values.next_action,
        result_payload,
      });
    },
    onSuccess: () => {
      message.success('任务已解决');
      setResolveOpen(false);
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const assignMutation = useMutation({
    mutationFn: (values: AssignForm) => api.assignTask(selectedId as number, values),
    onSuccess: () => {
      message.success('任务分配已更新');
      setAssignOpen(false);
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const reparseMutation = useMutation({
    mutationFn: () => api.reparseTask(selectedId as number, { mode: 'field_extract', reason: '前端人工复核重解析' }),
    onSuccess: () => {
      message.success('已触发重新解析');
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const draftReplyMutation = useMutation({
    mutationFn: () => {
      const detail = detailQuery.data as ManualTaskDetail;
      return api.draftReply(detail.ticket_context.ticket.id, {
        reply_type: 'followup',
        related_email_id: detail.task.email_id ?? detail.ticket_context.source_email?.id,
        language: 'zh-CN',
        missing_fields: detail.ticket_context.ticket.missing_fields ?? undefined,
      });
    },
    onSuccess: () => {
      message.success('追问草稿已生成');
      invalidateWorkbench();
      void queryClient.invalidateQueries({ queryKey: ['replies'] });
    },
    onError: handleMutationError,
  });
  const validateSnMutation = useMutation({
    mutationFn: () => api.validateTicketSn((detailQuery.data as ManualTaskDetail).ticket_context.ticket.id),
    onSuccess: () => {
      message.success('SN 校验完成');
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const patchFieldsMutation = useMutation({
    mutationFn: (values: TicketFieldForm) => {
      const detail = detailQuery.data as ManualTaskDetail;
      return api.patchTicketFields(detail.ticket_context.ticket.id, {
        version: detail.ticket_context.ticket.version,
        fields: values,
        reason: '前端人工复核修正字段',
      });
    },
    onSuccess: () => {
      message.success('字段已保存');
      setFieldOpen(false);
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const patchItemsMutation = useMutation({
    mutationFn: (values: TicketItemForm) => {
      const detail = detailQuery.data as ManualTaskDetail;
      const item = editingItem ? { id: editingItem.id, ...values } : values;
      return api.patchTicketItems(detail.ticket_context.ticket.id, { items: [item], reason: '前端人工复核修正明细' });
    },
    onSuccess: () => {
      message.success('明细已保存');
      setItemOpen(false);
      setEditingItem(null);
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const applyParseMutation = useMutation({
    mutationFn: ({ id, action }: { id: number; action: 'apply' | 'reject' }) => api.applyParseResult(id, {
      action,
      reason: action === 'reject' ? '前端人工复核拒绝解析候选' : '前端人工复核采纳解析候选',
    }),
    onSuccess: () => {
      message.success('解析候选状态已更新');
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });

  const openItemEditor = (item?: TicketLine) => {
    setEditingItem(item ?? null);
    setItemOpen(true);
  };

  const taskColumns: ColumnsType<ManualTask> = [
    {
      title: '任务',
      dataIndex: 'task_type',
      render: (_, record) => (
        <div className="queue-task-cell">
          <Typography.Text strong>{record.task_type}</Typography.Text>
          <Typography.Text type="secondary">{compactText(record.trigger_reason || record.description, '无触发说明')}</Typography.Text>
          <Typography.Text type="secondary">{formatTime(record.created_at)}</Typography.Text>
        </div>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 90, render: (value: string) => <StatusTag value={value} kind="task" /> },
    { title: '优先级', dataIndex: 'priority', width: 90, render: (value: string) => <StatusTag value={value} kind="priority" /> },
    { title: '处理人', dataIndex: 'claimed_by_user_id', width: 90, render: (_, record) => record.claimed_by_user_id || record.assigned_user_id || '-' },
  ];

  return (
    <div className="page-stack">
      <PageTitle title="人工复核工作台" />
      <div className="manual-workbench-grid">
        <SectionPanel className="task-queue-panel">
          <Form<TaskFilters>
            layout="vertical"
            className="compact-filter"
            onFinish={(values) => {
              setPage(1);
              setFilters(values);
              setSelectedId(null);
            }}
          >
            <Form.Item name="status" label="任务状态">
              <Select allowClear options={taskStatusOptions} />
            </Form.Item>
            <Form.Item name="scope" label="任务范围" initialValue="mine">
              <Select allowClear options={visibleTaskScopeOptions} />
            </Form.Item>
            <Form.Item name="task_type" label="任务类型">
              <Input allowClear prefix={<SearchOutlined />} />
            </Form.Item>
            <Button htmlType="submit" type="primary" block>筛选</Button>
          </Form>
          <Table<ManualTask>
            rowKey="id"
            size="small"
            columns={taskColumns}
            dataSource={tasksQuery.data?.items ?? []}
            loading={tasksQuery.isFetching}
            locale={{ emptyText: tasksQuery.isError ? '复核任务加载失败' : '暂无复核任务' }}
            rowClassName={(record) => (record.id === selectedId ? 'is-selected' : '')}
            onRow={(record) => ({ onClick: () => setSelectedId(record.id) })}
            pagination={{ current: page, pageSize: 20, total: tasksQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false, size: 'small' }}
          />
        </SectionPanel>
        <SectionPanel className="evidence-panel">
          <ManualEvidencePane
            detail={detailQuery.data}
            loading={detailQuery.isFetching}
            onApplyParse={(id) => confirmAction('确认采纳该解析候选？', () => applyParseMutation.mutate({ id, action: 'apply' }))}
            onRejectParse={(id) => confirmAction('确认拒绝该解析候选？', () => applyParseMutation.mutate({ id, action: 'reject' }))}
          />
        </SectionPanel>
        <SectionPanel className="action-panel">
          <ManualActionPane
            detail={detailQuery.data}
            loading={detailQuery.isFetching}
            onClaim={(id) => confirmAction('确认领取该复核任务？', () => claimMutation.mutate(id))}
            onRelease={(id) => confirmAction('确认释放该复核任务？', () => releaseMutation.mutate(id))}
            onAssign={() => setAssignOpen(true)}
            onResolve={() => setResolveOpen(true)}
            onReparse={() => confirmAction('确认重新解析关联邮件？', () => reparseMutation.mutate())}
            onDraftReply={() => confirmAction('确认生成追问草稿？', () => draftReplyMutation.mutate())}
            onValidateSn={() => confirmAction('确认执行 SN 校验？', () => validateSnMutation.mutate())}
            onEditFields={() => setFieldOpen(true)}
            onEditItem={openItemEditor}
            claimLoading={claimMutation.isPending}
            releaseLoading={releaseMutation.isPending}
            assignLoading={assignMutation.isPending}
            reparseLoading={reparseMutation.isPending}
            draftLoading={draftReplyMutation.isPending}
            validateLoading={validateSnMutation.isPending}
            canAssign={canAssignTasks}
          />
        </SectionPanel>
      </div>
      <Modal title="编辑工单字段" open={fieldOpen} onCancel={() => setFieldOpen(false)} footer={null} destroyOnClose>
        {detailQuery.data ? (
          <Form<TicketFieldForm>
            layout="vertical"
            initialValues={{
              customer_code: detailQuery.data.ticket_context.ticket.customer_code ?? undefined,
              customer_name: detailQuery.data.ticket_context.ticket.customer_name ?? undefined,
              contact_person: detailQuery.data.ticket_context.ticket.contact_person ?? undefined,
              contact_phone: detailQuery.data.ticket_context.ticket.contact_phone ?? undefined,
              contact_email: detailQuery.data.ticket_context.ticket.contact_email ?? undefined,
              request_date: detailQuery.data.ticket_context.ticket.request_date ?? undefined,
              mailing_address: detailQuery.data.ticket_context.ticket.mailing_address ?? undefined,
              problem_description: detailQuery.data.ticket_context.ticket.problem_description ?? undefined,
              accessories: detailQuery.data.ticket_context.ticket.accessories ?? undefined,
            }}
            onFinish={(values) => patchFieldsMutation.mutate(values)}
          >
            <Form.Item label="客户代码" name="customer_code"><Input /></Form.Item>
            <Form.Item label="客户名称" name="customer_name"><Input /></Form.Item>
            <Form.Item label="联系人" name="contact_person"><Input /></Form.Item>
            <Form.Item label="联系电话" name="contact_phone"><Input /></Form.Item>
            <Form.Item label="联系邮箱" name="contact_email"><Input /></Form.Item>
            <Form.Item label="报修日期" name="request_date"><Input placeholder="YYYY-MM-DD" /></Form.Item>
            <Form.Item label="寄送地址" name="mailing_address"><Input /></Form.Item>
            <Form.Item label="问题描述" name="problem_description"><Input.TextArea rows={4} /></Form.Item>
            <Form.Item label="附件/配件" name="accessories"><Input /></Form.Item>
            <Button type="primary" htmlType="submit" loading={patchFieldsMutation.isPending}>保存</Button>
          </Form>
        ) : null}
      </Modal>
      <Modal
        title={editingItem ? '编辑工单明细' : '新增工单明细'}
        open={itemOpen}
        onCancel={() => {
          setItemOpen(false);
          setEditingItem(null);
        }}
        footer={null}
        destroyOnClose
      >
        <Form<TicketItemForm>
          layout="vertical"
          initialValues={{
            line_no: editingItem?.line_no,
            material_code: editingItem?.material_code ?? undefined,
            material_name: editingItem?.material_name ?? undefined,
            sn: editingItem?.sn ?? undefined,
            quantity: editingItem?.quantity ?? 1,
            failure_description: editingItem?.failure_description ?? undefined,
            failure_information: editingItem?.failure_information ?? undefined,
            data_info: editingItem?.data_info ?? undefined,
            remarks: editingItem?.remarks ?? undefined,
            accessories: editingItem?.accessories ?? undefined,
            manual_locked: editingItem?.manual_locked ?? false,
          }}
          onFinish={(values) => patchItemsMutation.mutate(values)}
        >
          <div className="two-column-grid">
            <Form.Item label="行号" name="line_no"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
            <Form.Item label="数量" name="quantity"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
          </div>
          <Form.Item label="SN" name="sn"><Input /></Form.Item>
          <Form.Item label="物料编码" name="material_code"><Input /></Form.Item>
          <Form.Item label="物料名称" name="material_name"><Input /></Form.Item>
          <Form.Item label="故障描述" name="failure_description"><Input.TextArea rows={3} /></Form.Item>
          <Form.Item label="故障信息" name="failure_information"><Input.TextArea rows={2} /></Form.Item>
          <Form.Item label="数据信息" name="data_info"><Input.TextArea rows={2} /></Form.Item>
          <Form.Item label="配件" name="accessories"><Input /></Form.Item>
          <Form.Item label="备注" name="remarks"><Input.TextArea rows={2} /></Form.Item>
          <Form.Item label="人工锁定" name="manual_locked"><Select options={lockOptions} /></Form.Item>
          <Button type="primary" htmlType="submit" loading={patchItemsMutation.isPending}>保存</Button>
        </Form>
      </Modal>
      <Modal title="完成复核任务" open={resolveOpen} onCancel={() => setResolveOpen(false)} footer={null} destroyOnClose>
        <Form<ResolveForm> layout="vertical" onFinish={(values) => confirmAction('确认完成复核任务？', () => resolveMutation.mutate(values))}>
          <Form.Item label="处理类型" name="resolution_type">
            <Select allowClear options={resolutionTypeOptions} />
          </Form.Item>
          <Form.Item label="后续动作" name="next_action" rules={[{ required: true }]}>
            <Select options={resolveActionOptions} />
          </Form.Item>
          <Form.Item label="处理结论" name="resolution" rules={[{ required: true }]}>
            <Input.TextArea rows={4} />
          </Form.Item>
          <Form.Item label="结构化结果 JSON/备注" name="result_payload_text">
            <Input.TextArea rows={3} placeholder='例如 {"fixed_fields":["sn"]}；非 JSON 将按备注保存' />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={resolveMutation.isPending}>
            提交
          </Button>
        </Form>
      </Modal>
      {canAssignTasks ? (
        <Modal title="分配/转派任务" open={assignOpen} onCancel={() => setAssignOpen(false)} footer={null} destroyOnClose>
          <Form<AssignForm> layout="vertical" onFinish={(values) => assignMutation.mutate(values)}>
            <Form.Item label="处理人" name="assigned_user_id">
              <Select
                allowClear
                placeholder="清空后任务回到未分配"
                options={(usersQuery.data?.items ?? []).map((user: UserAccount) => ({
                  value: user.id,
                  label: `${user.real_name}（${user.username}）`,
                }))}
              />
            </Form.Item>
            <Form.Item label="分配说明" name="reason">
              <Input.TextArea rows={3} />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={assignMutation.isPending}>保存分配</Button>
          </Form>
        </Modal>
      ) : null}
    </div>
  );
}

function ManualEvidencePane({
  detail,
  loading,
  onApplyParse,
  onRejectParse,
}: {
  detail?: ManualTaskDetail;
  loading: boolean;
  onApplyParse: (id: number) => void;
  onRejectParse: (id: number) => void;
}) {
  if (!detail && !loading) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="请选择左侧任务" />;
  }
  if (!detail) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="加载中" />;
  }

  const context = detail.ticket_context;
  const timelineEmails = context.email_timeline.length > 0 ? context.email_timeline : context.source_email ? [context.source_email] : [];
  const fieldAudits = context.field_evidence?.field_audits ?? [];

  return (
    <div className="drawer-stack">
      <Descriptions column={2} size="small" bordered>
        <Descriptions.Item label="工单号">{context.ticket.ticket_no}</Descriptions.Item>
        <Descriptions.Item label="工单状态"><StatusTag value={context.ticket.current_status_code} kind="ticket" /></Descriptions.Item>
        <Descriptions.Item label="任务类型">{detail.task.task_type}</Descriptions.Item>
        <Descriptions.Item label="任务状态"><StatusTag value={detail.task.status} kind="task" /></Descriptions.Item>
        <Descriptions.Item label="触发原因" span={2}>{detail.task.trigger_reason || '-'}</Descriptions.Item>
      </Descriptions>
      <Tabs
        items={[
          {
            key: 'mail',
            label: `邮件证据(${timelineEmails.length})`,
            children: timelineEmails.length ? (
              <Timeline items={timelineEmails.map((email) => ({ children: <EmailTimelineItem email={email} /> }))} />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ),
          },
          {
            key: 'attachments',
            label: `附件(${context.attachments.length})`,
            children: (
              <Table<Attachment>
                size="small"
                rowKey="id"
                dataSource={context.attachments}
                pagination={false}
                columns={[
                  { title: '文件名', dataIndex: 'file_name', ellipsis: true },
                  { title: '类型', dataIndex: 'content_type', width: 140, render: (value?: string) => value || '-' },
                  { title: '大小', dataIndex: 'file_size', width: 90, render: formatBytes },
                  { title: '解析', dataIndex: 'parse_status', width: 100, render: (value: string) => <StatusTag value={value} kind="parse" /> },
                ]}
                expandable={{
                  expandedRowRender: (record) => (
                    <div className="evidence-grid">
                      <div>
                        <Typography.Text strong>提取文本</Typography.Text>
                        <pre className="json-block">{record.extracted_text || '-'}</pre>
                      </div>
                      <div>
                        <Typography.Text strong>提取 JSON</Typography.Text>
                        <JsonBlock value={record.extracted_json} />
                      </div>
                    </div>
                  ),
                }}
              />
            ),
          },
          {
            key: 'parse',
            label: `解析候选(${context.parse_results.length})`,
            children: (
              <Table<ParseResult>
                size="small"
                rowKey="id"
                dataSource={context.parse_results}
                pagination={false}
                columns={[
                  { title: 'ID', dataIndex: 'id', width: 70 },
                  { title: '解析器', dataIndex: 'parser_type', width: 100 },
                  { title: '意图', dataIndex: 'intent_type', width: 120, render: (value?: string) => value || '-' },
                  { title: '置信度', dataIndex: 'confidence_score', width: 90, render: numberText },
                  { title: '应用状态', dataIndex: 'apply_status', width: 120, render: (value: string) => <StatusTag value={value} /> },
                  { title: '缺失字段', dataIndex: 'missing_fields', render: (value) => <JsonBlock value={value} /> },
                  {
                    title: '操作',
                    width: 130,
                    render: (_, record) => {
                      const handled = Boolean(record.apply_status && record.apply_status !== 'pending');
                      return (
                        <Space size={0}>
                          <Button type="link" size="small" disabled={handled || record.accepted} onClick={() => onApplyParse(record.id)}>采纳</Button>
                          <Button type="link" size="small" danger disabled={handled} onClick={() => onRejectParse(record.id)}>拒绝</Button>
                        </Space>
                      );
                    },
                  },
                ]}
                expandable={{
                  expandedRowRender: (record) => (
                    <div className="evidence-grid">
                      <div>
                        <Typography.Text strong>提取字段</Typography.Text>
                        <JsonBlock value={record.extracted_fields} />
                      </div>
                      <div>
                        <Typography.Text strong>提取明细</Typography.Text>
                        <JsonBlock value={record.extracted_items} />
                      </div>
                      <div>
                        <Typography.Text strong>证据</Typography.Text>
                        <JsonBlock value={record.evidence} />
                      </div>
                    </div>
                  ),
                }}
              />
            ),
          },
          {
            key: 'replies',
            label: `回复链(${context.reply_records.length})`,
            children: (
              <Table<ReplyRecord>
                size="small"
                rowKey="id"
                dataSource={context.reply_records}
                pagination={false}
                columns={[
                  { title: '类型', dataIndex: 'reply_type', width: 100 },
                  { title: '轮次', dataIndex: 'followup_round', width: 70 },
                  { title: '收件人', dataIndex: 'to_addresses', ellipsis: true },
                  { title: '审核', dataIndex: 'review_status', width: 100, render: (value: string) => <StatusTag value={value} kind="review" /> },
                  { title: '发送', dataIndex: 'send_status', width: 100, render: (value: string) => <StatusTag value={value} /> },
                ]}
                expandable={{
                  expandedRowRender: (record) => (
                    <div className="evidence-grid">
                      <div>
                        <Typography.Text strong>草稿内容</Typography.Text>
                        <pre className="json-block">{record.draft_body || '-'}</pre>
                      </div>
                      <div>
                        <Typography.Text strong>最终内容</Typography.Text>
                        <pre className="json-block">{record.final_body || '-'}</pre>
                      </div>
                    </div>
                  ),
                }}
              />
            ),
          },
          {
            key: 'audit',
            label: `字段证据(${fieldAudits.length})`,
            children: (
              <div className="drawer-stack">
                <div className="two-column-grid">
                  <div>
                    <Typography.Title level={5}>缺失字段</Typography.Title>
                    <JsonBlock value={context.ticket.missing_fields} />
                  </div>
                  <div>
                    <Typography.Title level={5}>冲突字段</Typography.Title>
                    <JsonBlock value={context.ticket.conflict_fields} />
                  </div>
                </div>
                <Table<FieldAuditLog>
                  size="small"
                  rowKey="id"
                  dataSource={fieldAudits}
                  pagination={false}
                  columns={[
                    { title: '字段', dataIndex: 'field_name', width: 140 },
                    { title: '旧值', dataIndex: 'old_value', ellipsis: true, render: (value?: string) => value || '-' },
                    { title: '新值', dataIndex: 'new_value', ellipsis: true, render: (value?: string) => value || '-' },
                    { title: '来源', dataIndex: 'source_type', width: 90 },
                    { title: '时间', dataIndex: 'created_at', width: 150, render: formatTime },
                  ]}
                />
              </div>
            ),
          },
        ]}
      />
    </div>
  );
}

function ManualActionPane({
  detail,
  loading,
  onClaim,
  onRelease,
  onAssign,
  onResolve,
  onReparse,
  onDraftReply,
  onValidateSn,
  onEditFields,
  onEditItem,
  claimLoading,
  releaseLoading,
  assignLoading,
  reparseLoading,
  draftLoading,
  validateLoading,
  canAssign,
}: {
  detail?: ManualTaskDetail;
  loading: boolean;
  onClaim: (id: number) => void;
  onRelease: (id: number) => void;
  onAssign: () => void;
  onResolve: () => void;
  onReparse: () => void;
  onDraftReply: () => void;
  onValidateSn: () => void;
  onEditFields: () => void;
  onEditItem: (item?: TicketLine) => void;
  claimLoading: boolean;
  releaseLoading: boolean;
  assignLoading: boolean;
  reparseLoading: boolean;
  draftLoading: boolean;
  validateLoading: boolean;
  canAssign: boolean;
}) {
  if (!detail && !loading) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="请选择任务后处理" />;
  }
  if (!detail) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="加载中" />;
  }

  const { task, ticket_context: context } = detail;
  const suggestions = buildSuggestions(detail);
  const canClaim = task.status === 'pending' || task.status === 'assigned';
  const canRelease = task.status === 'claimed' || task.status === 'assigned';
  const canResolve = task.status !== 'resolved' && task.status !== 'closed';

  return (
    <div className="drawer-stack">
      <Descriptions column={1} size="small" bordered>
        <Descriptions.Item label="任务状态"><StatusTag value={task.status} kind="task" /></Descriptions.Item>
        <Descriptions.Item label="优先级"><StatusTag value={task.priority} kind="priority" /></Descriptions.Item>
        <Descriptions.Item label="工单状态"><StatusTag value={context.ticket.current_status_code} kind="ticket" /></Descriptions.Item>
        <Descriptions.Item label="客户">{context.ticket.customer_name || '-'}</Descriptions.Item>
        <Descriptions.Item label="联系人">{context.ticket.contact_person || context.ticket.contact_email || '-'}</Descriptions.Item>
        <Descriptions.Item label="问题描述">{compactText(context.ticket.problem_description, '-')}</Descriptions.Item>
      </Descriptions>
      <div className="suggestion-list">
        <Typography.Title level={5}>建议动作</Typography.Title>
        {suggestions.map((item) => <Typography.Text key={item}>• {item}</Typography.Text>)}
      </div>
      <div className="action-button-grid">
        <Button disabled={!canClaim} loading={claimLoading} onClick={() => onClaim(task.id)}>领取</Button>
        <Button disabled={!canRelease} loading={releaseLoading} onClick={() => onRelease(task.id)}>释放</Button>
        {canAssign ? <Button loading={assignLoading} onClick={onAssign}>分配/转派</Button> : null}
        <Button icon={<EditOutlined />} onClick={onEditFields}>字段修正</Button>
        <Button icon={<FileAddOutlined />} onClick={() => onEditItem()}>新增明细</Button>
        <Button icon={<CheckCircleOutlined />} loading={validateLoading} onClick={onValidateSn}>SN 校验</Button>
        <Button icon={<SyncOutlined />} loading={reparseLoading} onClick={onReparse}>重新解析</Button>
        <Button icon={<MailOutlined />} loading={draftLoading} onClick={onDraftReply}>生成追问</Button>
        <Button type="primary" disabled={!canResolve} onClick={onResolve}>完成任务</Button>
      </div>
      <Tabs
        size="small"
        items={[
          {
            key: 'fields',
            label: '工单字段',
            children: (
              <Descriptions column={1} size="small" bordered>
                <Descriptions.Item label="客户代码">{context.ticket.customer_code || '-'}</Descriptions.Item>
                <Descriptions.Item label="客户名称">{context.ticket.customer_name || '-'}</Descriptions.Item>
                <Descriptions.Item label="联系电话">{context.ticket.contact_phone || '-'}</Descriptions.Item>
                <Descriptions.Item label="联系邮箱">{context.ticket.contact_email || '-'}</Descriptions.Item>
                <Descriptions.Item label="寄送地址">{context.ticket.mailing_address || '-'}</Descriptions.Item>
              </Descriptions>
            ),
          },
          {
            key: 'items',
            label: `明细(${context.items.length})`,
            children: (
              <Table<TicketLine>
                size="small"
                rowKey="id"
                dataSource={context.items}
                pagination={false}
                columns={[
                  { title: '行', dataIndex: 'line_no', width: 48 },
                  { title: 'SN', dataIndex: 'sn', width: 120, render: (value?: string) => value || '-' },
                  { title: '物料', dataIndex: 'material_code', ellipsis: true, render: (value?: string) => value || '-' },
                  { title: '操作', width: 70, render: (_, record) => <Button type="link" size="small" onClick={() => onEditItem(record)}>编辑</Button> },
                ]}
              />
            ),
          },
        ]}
      />
    </div>
  );
}

function EmailTimelineItem({ email }: { email: EmailItem }) {
  return (
    <div className="timeline-item">
      <div className="timeline-heading">
        <Typography.Text strong>{email.subject || '(无主题)'}</Typography.Text>
        <StatusTag value={email.parse_status} kind="parse" />
      </div>
      <div className="timeline-meta">
        <span>{email.mail_direction}</span>
        <span>{email.from_address}</span>
        <span>{formatTime(email.received_at || email.sent_at)}</span>
      </div>
      <Typography.Paragraph ellipsis={{ rows: 3 }} className="timeline-body">
        {email.latest_reply_segment || email.clean_body || '-'}
      </Typography.Paragraph>
    </div>
  );
}

function buildSuggestions(detail: ManualTaskDetail) {
  const suggestions: string[] = [];
  const ticket = detail.ticket_context.ticket;
  if (ticket.missing_fields && Object.keys(ticket.missing_fields).length > 0) {
    suggestions.push('优先核对缺失字段，必要时生成追问草稿。');
  }
  if (ticket.conflict_fields && Object.keys(ticket.conflict_fields).length > 0) {
    suggestions.push('存在冲突字段，建议比对邮件证据后人工修正。');
  }
  if (detail.ticket_context.parse_results.some((item) => !item.accepted)) {
    suggestions.push('存在未采纳解析候选，可核对证据后采纳。');
  }
  if (!suggestions.length) {
    suggestions.push('上下文较完整，可校验 SN 后完成任务或流转工单。');
  }
  return suggestions;
}

function formatBytes(value?: number | null) {
  if (!value) return '-';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
