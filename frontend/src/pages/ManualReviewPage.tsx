import {
  CheckCircleOutlined,
  DownloadOutlined,
  EditOutlined,
  FileAddOutlined,
  MailOutlined,
  SearchOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Button,
  DatePicker,
  Descriptions,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Timeline,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api, apiErrorMessage } from '../api/client';
import ContentPreviewButton from '../components/ContentPreviewButton';
import CopyableField from '../components/CopyableField';
import ErrorResult from '../components/ErrorResult';
import JsonBlock from '../components/JsonBlock';
import PageTitle from '../components/PageTitle';
import ParseResultSelectionModal from '../components/ParseResultSelectionModal';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import TicketFieldEditor, { type TicketFieldForm } from '../components/TicketFieldEditor';
import TicketItemEditor, { type TicketItemForm } from '../components/TicketItemEditor';
import type {
  Attachment,
  EmailItem,
  FieldAuditLog,
  JsonRecord,
  ManualTask,
  ManualTaskDetail,
  PageData,
  ParseResult,
  ReplyRecord,
  TicketLine,
} from '../types/api';
import { ARCHIVE_DOWNLOAD_WARNING, attachmentTypeLabel, isEngineeringReference } from '../utils/attachments';
import { filtersWithDateRange } from '../utils/filters';
import { compactText, formatFileSizeKb, formatTime, numberText } from '../utils/format';
type TaskFilters = {
  status?: string;
  task_type?: string;
  scope?: string;
  priority?: string;
  category?: string;
  date_range?: unknown;
};

type ResolveForm = {
  resolution: string;
  resolution_type?: string;
  next_action: string;
  target_first_intent?: 'new_repair' | 'thread_new_repair' | 'customer_supplement';
  result_payload_text?: string;
};

type PolicyOverrideForm = {
  charge_status: 'free' | 'annual_contract' | 'chargeable';
  customer_scope: 'domestic' | 'overseas';
  reason: string;
};

type ReturnRouteForm = {
  return_location: 'beijing' | 'tianjin';
  reason: string;
};

const taskStatusOptions = [
  { value: 'pending', label: '待分配' },
  { value: 'assigned', label: '已分配待处理' },
  { value: 'claimed', label: '处理中' },
  { value: 'resolved', label: '已解决' },
];

const resolveActionOptions = [
  { value: 'promote_to_first', label: '晋升为 FIRST 自动报修' },
  { value: 'finish_external_handling', label: '完成邮件级人工处理' },
  { value: 'resolve_manual_business', label: '完成 SECOND 人工业务工单' },
  { value: 'transition_ready_for_export', label: '进入可导出' },
  { value: 'generate_followup', label: '生成追问' },
  { value: 'wait_customer_info', label: '等待客户补充' },
  { value: 'reparse', label: '重新解析' },
  { value: 'keep_manual_review', label: '保持人工复核' },
];

const taskScopeOptions = [
  { value: 'mine', label: '我的待处理' },
  { value: 'all', label: '全部待处理' },
];

const taskPriorityOptions = [
  { value: 'high', label: '高' },
  { value: 'normal', label: '普通' },
  { value: 'low', label: '低' },
];

const resolutionTypeOptions = [
  { value: 'field_fixed', label: '字段已修正' },
  { value: 'sn_checked', label: 'SN 已核验' },
  { value: 'need_followup', label: '需要追问' },
  { value: 'false_positive', label: '无需处理' },
  { value: 'system_exception', label: '系统异常' },
  { value: 'other', label: '其他' },
];

export default function ManualReviewPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [filters, setFilters] = useState<Record<string, unknown>>({});
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<number | null>(() => {
    const value = Number(searchParams.get('task_id'));
    return Number.isInteger(value) && value > 0 ? value : null;
  });
  const [resolveOpen, setResolveOpen] = useState(false);
  const [fieldOpen, setFieldOpen] = useState(false);
  const [itemOpen, setItemOpen] = useState(false);
  const [editingItem, setEditingItem] = useState<TicketLine | null>(null);
  const [partialParse, setPartialParse] = useState<ParseResult | null>(null);
  const [policyOverrideOpen, setPolicyOverrideOpen] = useState(false);
  const [returnRouteItem, setReturnRouteItem] = useState<TicketLine | null>(null);
  const [filterForm] = Form.useForm<TaskFilters>();
  const queryClient = useQueryClient();
  const visibleTaskScopeOptions = taskScopeOptions;
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
        target_first_intent: values.target_first_intent,
      });
    },
    onSuccess: () => {
      message.success('任务已解决');
      setResolveOpen(false);
      invalidateWorkbench();
      // Auto-navigate to next pending task
      setTimeout(() => {
        const cached = queryClient.getQueryData<PageData<ManualTask>>(['manual-tasks', filters, page]);
        const items = cached?.items ?? [];
        const idx = items.findIndex((t) => t.id === selectedId);
        if (idx >= 0 && idx < items.length - 1) {
          setSelectedId(items[idx + 1].id);
        } else if (items.length > 0 && idx >= 0) {
          setPage((p) => p + 1);
        } else {
          message.success('所有任务已处理完毕');
          setSelectedId(null);
        }
      }, 500);
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
      return api.draftReply(detail.ticket_context!.ticket.id, {
        reply_type: 'followup',
        related_email_id: detail.task.email_id ?? detail.ticket_context!.source_email?.id,
        language: 'zh-CN',
        missing_fields: detail.ticket_context!.ticket.missing_fields ?? undefined,
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
    mutationFn: () => api.validateTicketSn((detailQuery.data as ManualTaskDetail).ticket_context!.ticket.id),
    onSuccess: () => {
      message.success('SN 校验完成');
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const patchFieldsMutation = useMutation({
    mutationFn: (values: TicketFieldForm) => {
      const detail = detailQuery.data as ManualTaskDetail;
      return api.patchTicketFields(detail.ticket_context!.ticket.id, {
        version: detail.ticket_context!.ticket.version,
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
      return api.patchTicketItems(detail.ticket_context!.ticket.id, { items: [item], reason: '前端人工复核修正明细' });
    },
    onSuccess: () => {
      message.success('明细已保存');
      setItemOpen(false);
      setEditingItem(null);
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const resolvePolicyMutation = useMutation({
    mutationFn: () => api.resolveTicketPolicy(
      (detailQuery.data as ManualTaskDetail).ticket_context!.ticket.id,
    ),
    onSuccess: () => {
      message.success('客户范围与服务政策已重新解析');
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const overridePolicyMutation = useMutation({
    mutationFn: (values: PolicyOverrideForm) => api.overrideTicketPolicy(
      (detailQuery.data as ManualTaskDetail).ticket_context!.ticket.id,
      values,
    ),
    onSuccess: () => {
      message.success('当前工单政策已人工确认');
      setPolicyOverrideOpen(false);
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const resolveRoutesMutation = useMutation({
    mutationFn: () => api.resolveReturnRoutes(
      (detailQuery.data as ManualTaskDetail).ticket_context!.ticket.id,
    ),
    onSuccess: () => {
      message.success('维修寄回地址已重新匹配');
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const selectRouteMutation = useMutation({
    mutationFn: (values: ReturnRouteForm) => api.selectReturnRoute(
      (detailQuery.data as ManualTaskDetail).ticket_context!.ticket.id,
      returnRouteItem?.id as number,
      values,
    ),
    onSuccess: () => {
      message.success('维修寄回地点与地址快照已保存');
      setReturnRouteItem(null);
      invalidateWorkbench();
    },
    onError: handleMutationError,
  });
  const applyParseMutation = useMutation({
    mutationFn: ({ id, action, selected_fields, selected_item_indices }: { id: number; action: 'apply' | 'partial_apply' | 'reject'; selected_fields?: string[]; selected_item_indices?: number[] }) => api.applyParseResult(id, {
      action,
      selected_fields,
      selected_item_indices,
      reason: action === 'reject' ? '前端人工复核拒绝解析候选' : '前端人工复核采纳解析候选',
    }),
    onSuccess: () => {
      message.success('解析候选状态已更新');
      setPartialParse(null);
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
      ellipsis: true,
      render: (_: unknown, record: ManualTask) => (
        <div style={{ lineHeight: 1.5 }}>
          <div>
            {record.ticket_id ? (
              <Button type="link" size="small" style={{ padding: 0 }}
                onClick={(e) => { e.stopPropagation(); navigate(`/tickets?ticket_id=${record.ticket_id}`); }}>
                #{record.ticket_id}
              </Button>
            ) : <Tag color="purple">邮件级任务</Tag>}
            <Tag style={{ marginLeft: 4 }}>{record.task_type}</Tag>
          </div>
          <div>
            <Typography.Text style={{ fontSize: 12 }}>
              {record.trigger_reason || record.description || '-'}
            </Typography.Text>
          </div>
        </div>
      ),
    },
    {
      title: '任务类型', dataIndex: 'task_type', width: 120,
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: '状态', dataIndex: 'status', width: 80,
      render: (v: string) => <StatusTag value={v} kind="task" />,
    },
    {
      title: '优先级', dataIndex: 'priority', width: 70,
      render: (v: string) => <StatusTag value={v} kind="priority" />,
    },
    {
      title: '负责人', dataIndex: 'assigned_user_id', width: 100,
      render: (v: number | null) => v ? `用户#${v}` : <Typography.Text type="secondary">未分配</Typography.Text>,
    },
  ];

  return (
    <div className="page-stack">
      <PageTitle title="人工复核工作台" />
      <Space wrap>
        <Button onClick={() => { setPage(1); setSelectedId(null); setFilters({ scope: 'mine', status: 'pending' }); }}>我的待处理</Button>
        <Button onClick={() => { setPage(1); setSelectedId(null); setFilters({ scope: 'all', status: 'pending' }); }}>全部待处理</Button>
        <Button onClick={() => { setPage(1); setSelectedId(null); setFilters({ category: 'rma', scope: 'all', status: 'pending' }); }}>RMA 异常</Button>
        <Button onClick={() => { setPage(1); setSelectedId(null); setFilters({ category: 'sql', scope: 'all', status: 'pending' }); }}>SQL 异常</Button>
      </Space>
      <div className="manual-workbench-grid">
        {/* LEFT: Task queue + filters */}
        <SectionPanel className="task-queue-panel">
          <Form<TaskFilters>
            form={filterForm}
            layout="vertical"
            className="compact-filter"
            onFinish={(values) => {
              setPage(1);
              setSelectedId(null);
              setFilters(filtersWithDateRange(values, 'date_range', 'created_start', 'created_end'));
            }}
          >
            <Space direction="vertical" style={{ width: '100%' }}>
              <Form.Item name="scope" noStyle>
                <Select allowClear placeholder="任务范围" options={visibleTaskScopeOptions} />
              </Form.Item>
              <Form.Item name="status" noStyle>
                <Select allowClear placeholder="任务状态" options={taskStatusOptions} />
              </Form.Item>
              <Form.Item name="priority" noStyle>
                <Select allowClear placeholder="优先级" options={taskPriorityOptions} />
              </Form.Item>
              <Form.Item name="task_type" noStyle>
                <Input allowClear placeholder="任务类型" />
              </Form.Item>
              <Form.Item name="date_range" noStyle>
                <DatePicker.RangePicker allowClear style={{ width: '100%' }} />
              </Form.Item>
            </Space>
            <Space direction="vertical" style={{ width: '100%', marginTop: 8 }}>
              <Button htmlType="submit" type="primary" block>筛选</Button>
              <Button block onClick={() => { filterForm.resetFields(); setPage(1); setSelectedId(null); setFilters({}); }}>重置</Button>
            </Space>
          </Form>
          <Table<ManualTask>
            rowKey="id"
            size="small"
            columns={taskColumns}
            dataSource={tasksQuery.data?.items ?? []}
            loading={tasksQuery.isFetching}
            locale={{
              emptyText: tasksQuery.isError
                ? <ErrorResult message={apiErrorMessage(tasksQuery.error)} onRetry={() => tasksQuery.refetch()} />
                : '暂无复核任务'
            }}
            rowClassName={(record) => (record.id === selectedId ? 'is-selected' : '')}
            onRow={(record) => ({ onClick: () => setSelectedId(record.id) })}
            pagination={{ current: page, pageSize: 20, total: tasksQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false, size: 'small' }}
          />
        </SectionPanel>

        {/* RIGHT: Evidence + Actions combined */}
        {detailQuery.data ? (
          <div style={{ minWidth: 0, maxHeight: 'calc(100vh - 200px)', overflow: 'auto' }}>
            <SectionPanel className="evidence-panel">
            <ManualEvidencePane
              detail={detailQuery.data}
              loading={detailQuery.isFetching}
              onApplyParse={(id) => confirmAction('确认采纳该解析候选？', () => applyParseMutation.mutate({ id, action: 'apply' }))}
              onPartialParse={setPartialParse}
              onRejectParse={(id) => confirmAction('确认拒绝该解析候选？', () => applyParseMutation.mutate({ id, action: 'reject' }))}
              onEditFields={() => setFieldOpen(true)}
              onEditItem={openItemEditor}
              onResolve={() => setResolveOpen(true)}
              onReparse={() => confirmAction('确认重新解析关联邮件？', () => reparseMutation.mutate())}
              onDraftReply={() => confirmAction('确认生成追问草稿？', () => draftReplyMutation.mutate())}
              onValidateSn={() => confirmAction('确认执行 SN 校验？', () => validateSnMutation.mutate())}
              onResolvePolicy={() => resolvePolicyMutation.mutate()}
              onOverridePolicy={() => setPolicyOverrideOpen(true)}
              onResolveRoutes={() => resolveRoutesMutation.mutate()}
              onSelectRoute={setReturnRouteItem}
              reparseLoading={reparseMutation.isPending}
              draftLoading={draftReplyMutation.isPending}
              validateLoading={validateSnMutation.isPending}
              resolvePolicyLoading={resolvePolicyMutation.isPending}
              resolveRoutesLoading={resolveRoutesMutation.isPending}
            />
            </SectionPanel>
          </div>
        ) : detailQuery.isFetching ? (
          <SectionPanel><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="加载中" /></SectionPanel>
        ) : tasksQuery.data?.items.length ? (
          <SectionPanel><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="请选择左侧任务" /></SectionPanel>
        ) : null}
      </div>
      <Modal title="编辑工单字段" open={fieldOpen} onCancel={() => setFieldOpen(false)} footer={null} destroyOnClose>
        {detailQuery.data?.ticket_context ? (
          <TicketFieldEditor
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
            onSubmit={async (values) => {
              await patchFieldsMutation.mutateAsync(values);
              setFieldOpen(false);
            }}
            loading={patchFieldsMutation.isPending}
            onCancel={() => setFieldOpen(false)}
          />
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
        <TicketItemEditor
          editingItem={editingItem}
          onSubmit={async (values) => {
            await patchItemsMutation.mutateAsync(values);
            setItemOpen(false);
            setEditingItem(null);
          }}
          loading={patchItemsMutation.isPending}
          onCancel={() => {
            setItemOpen(false);
            setEditingItem(null);
          }}
        />
      </Modal>
      <Modal
        title="人工确认当前工单政策"
        open={policyOverrideOpen}
        onCancel={() => setPolicyOverrideOpen(false)}
        footer={null}
        destroyOnClose
      >
        <Typography.Paragraph type="secondary">
          仅修改当前工单快照，不会反向修改客户政策主数据。
        </Typography.Paragraph>
        <Form<PolicyOverrideForm>
          layout="vertical"
          initialValues={{
            charge_status: 'chargeable',
            customer_scope: detailQuery.data?.ticket_context?.ticket.customer_scope || 'domestic',
          }}
          onFinish={(values) => overridePolicyMutation.mutate(values)}
        >
          <Form.Item label="客户范围" name="customer_scope" rules={[{ required: true }]}>
            <Select options={[
              { value: 'domestic', label: '国内客户' },
              { value: 'overseas', label: '海外客户' },
            ]} />
          </Form.Item>
          <Form.Item label="收费状态" name="charge_status" rules={[{ required: true }]}>
            <Select options={[
              { value: 'free', label: '免费' },
              { value: 'annual_contract', label: '包年/合同内' },
              { value: 'chargeable', label: '收费' },
            ]} />
          </Form.Item>
          <Form.Item label="确认依据" name="reason" rules={[{ required: true, min: 3, max: 500 }]}>
            <Input.TextArea rows={4} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={overridePolicyMutation.isPending}>
            保存当前工单政策
          </Button>
        </Form>
      </Modal>
      <Modal
        title={`人工选择维修寄回地点${returnRouteItem ? `（明细 ${returnRouteItem.line_no}）` : ''}`}
        open={returnRouteItem !== null}
        onCancel={() => setReturnRouteItem(null)}
        footer={null}
        destroyOnClose
      >
        <Form<ReturnRouteForm>
          layout="vertical"
          onFinish={(values) => selectRouteMutation.mutate(values)}
        >
          <Form.Item label="维修寄回地点" name="return_location" rules={[{ required: true }]}>
            <Select options={[
              { value: 'beijing', label: '北京' },
              { value: 'tianjin', label: '天津' },
            ]} />
          </Form.Item>
          <Form.Item label="人工选择依据" name="reason" rules={[{ required: true, min: 3, max: 500 }]}>
            <Input.TextArea rows={4} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={selectRouteMutation.isPending}>
            保存地点与地址快照
          </Button>
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
          <Form.Item label="晋升后的 FIRST 意图" name="target_first_intent">
            <Select allowClear placeholder="仅“晋升为 FIRST”时必选" options={[
              { value: 'new_repair', label: '新报修' },
              { value: 'thread_new_repair', label: '回复链新报修' },
              { value: 'customer_supplement', label: '客户补充报修信息' },
            ]} />
          </Form.Item>
          <Form.Item label="结构化结果 JSON/备注" name="result_payload_text">
            <Input.TextArea rows={3} placeholder='例如 {"fixed_fields":["sn"]}；非 JSON 将按备注保存' />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={resolveMutation.isPending}>
            提交
          </Button>
        </Form>
      </Modal>
      <ParseResultSelectionModal
        open={Boolean(partialParse)}
        parseResult={partialParse}
        loading={applyParseMutation.isPending}
        onCancel={() => setPartialParse(null)}
        onConfirm={(selection) => partialParse && applyParseMutation.mutate({ id: partialParse.id, action: 'partial_apply', ...selection })}
      />
    </div>
  );
}

function ManualEvidencePane({
  detail,
  loading,
  onApplyParse,
  onPartialParse,
  onRejectParse,
  onEditFields,
  onEditItem,
  onResolve,
  onReparse,
  onDraftReply,
  onValidateSn,
  onResolvePolicy,
  onOverridePolicy,
  onResolveRoutes,
  onSelectRoute,
  reparseLoading,
  draftLoading,
  validateLoading,
  resolvePolicyLoading,
  resolveRoutesLoading,
}: {
  detail?: ManualTaskDetail;
  loading: boolean;
  onApplyParse: (id: number) => void;
  onPartialParse: (record: ParseResult) => void;
  onRejectParse: (id: number) => void;
  onEditFields: () => void;
  onEditItem: (item?: TicketLine) => void;
  onResolve: () => void;
  onReparse: () => void;
  onDraftReply: () => void;
  onValidateSn: () => void;
  onResolvePolicy: () => void;
  onOverridePolicy: () => void;
  onResolveRoutes: () => void;
  onSelectRoute: (item: TicketLine) => void;
  reparseLoading: boolean;
  draftLoading: boolean;
  validateLoading: boolean;
  resolvePolicyLoading: boolean;
  resolveRoutesLoading: boolean;
}) {
  const attachmentDownloadMutation = useMutation({
    mutationFn: (attachmentId: number) => api.attachmentDownloadUrl(attachmentId),
    onSuccess: (data) => {
      window.open(data.url, '_blank', 'noopener,noreferrer');
    },
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const downloadAttachment = (record: Attachment) => {
    if (!isEngineeringReference(record)) {
      attachmentDownloadMutation.mutate(record.id);
      return;
    }
    Modal.confirm({
      title: '下载工程辅助资料？',
      content: ARCHIVE_DOWNLOAD_WARNING,
      okText: '继续下载',
      cancelText: '取消',
      onOk: () => attachmentDownloadMutation.mutate(record.id),
    });
  };

  if (!detail && !loading) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="请选择左侧任务" />;
  }
  if (!detail) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="加载中" />;
  }

  if (!detail.ticket_context) {
    const email = detail.email_context;
    return (
      <div className="drawer-stack">
        <Descriptions column={2} size="small" bordered>
          <Descriptions.Item label="任务类型">{detail.task.task_type}</Descriptions.Item>
          <Descriptions.Item label="任务状态"><StatusTag value={detail.task.status} kind="task" /></Descriptions.Item>
          <Descriptions.Item label="处理层级">{email?.handling_level || '-'}</Descriptions.Item>
          <Descriptions.Item label="邮件意图">{email?.intent_type || '-'}</Descriptions.Item>
          <Descriptions.Item label="邮件主题" span={2}>{email?.subject || '-'}</Descriptions.Item>
          <Descriptions.Item label="发件人">{email?.from_address || '-'}</Descriptions.Item>
          <Descriptions.Item label="Message-ID"><CopyableField value={email?.message_id || ''} /></Descriptions.Item>
          <Descriptions.Item label="分类原因" span={2}>{email?.classification_reason_code || detail.task.trigger_reason || '-'}</Descriptions.Item>
          <Descriptions.Item label="恢复动作" span={2}>{detail.task.recovery_action || '-'}</Descriptions.Item>
        </Descriptions>
        <Typography.Title level={5}>邮件正文</Typography.Title>
        <pre className="json-block">{email?.latest_reply_segment || email?.clean_body || '-'}</pre>
        <Typography.Title level={5}>附件</Typography.Title>
        <Table<Attachment>
          size="small"
          rowKey="id"
          pagination={false}
          dataSource={email?.attachments || []}
          columns={[
            { title: '文件名', dataIndex: 'file_name', ellipsis: true },
            { title: '类型', dataIndex: 'content_type', width: 150, render: (value?: string) => value || '-' },
            { title: '解析状态', dataIndex: 'parse_status', width: 120, render: (value: string) => <StatusTag value={value} kind="parse" /> },
            { title: '操作', width: 90, render: (_: unknown, record: Attachment) => <ContentPreviewButton kind="attachment" id={record.id} disabled={!record.oss_object_id} /> },
          ]}
        />
        <Space wrap>
          <Button loading={reparseLoading} onClick={onReparse}>重新解析邮件</Button>
          <Button type="primary" onClick={onResolve}>记录人工结论</Button>
        </Space>
      </div>
    );
  }

  const context = detail.ticket_context;
  const timelineEmails = context.email_timeline.length > 0 ? context.email_timeline : context.source_email ? [context.source_email] : [];
  const fieldAudits = context.field_evidence?.field_audits ?? [];

  return (
    <div className="drawer-stack">
      <Descriptions column={2} size="small" bordered>
        <Descriptions.Item label="工单号"><CopyableField value={context.ticket.ticket_no} /></Descriptions.Item>
        <Descriptions.Item label="工单状态"><StatusTag value={context.ticket.current_status_code} kind="ticket" /></Descriptions.Item>
        <Descriptions.Item label="任务类型">{detail.task.task_type}</Descriptions.Item>
        <Descriptions.Item label="任务状态"><StatusTag value={detail.task.status} kind="task" /></Descriptions.Item>
        <Descriptions.Item label="触发原因" span={2}>{detail.task.trigger_reason || '-'}</Descriptions.Item>
      </Descriptions>
      <Tabs
        items={[
          {
            key: 'mail',
            label: '邮件证据',
            children: (
              <div className="drawer-stack">
                {timelineEmails.length ? (
                  <Timeline items={timelineEmails.map((email) => ({ children: <EmailTimelineItem email={email} /> }))} />
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
                )}
                <Typography.Title level={5}>附件</Typography.Title>
                <Table<Attachment>
                  size="small" rowKey="id" dataSource={context.attachments} pagination={false}
                  columns={[
                    { title: '文件名', dataIndex: 'file_name', ellipsis: true },
                    { title: '类型', dataIndex: 'content_type', width: 140, render: (v?: string) => v || '-' },
                    { title: '附件类型', width: 170, render: (_: unknown, r: Attachment) => attachmentTypeLabel(r) },
                    { title: '发送时间', dataIndex: 'sent_at', width: 150, render: formatTime },
                    { title: '大小', dataIndex: 'file_size_kb', width: 90, render: (_v: unknown, r: Attachment) => formatFileSizeKb(r.file_size_kb, r.file_size) },
                    { title: '解析', dataIndex: 'parse_status', width: 150, render: (v: string, r: Attachment) => <StatusTag value={isEngineeringReference(r) ? 'engineering_reference_stored' : v} kind="parse" /> },
                    { title: '安全提示', width: 140, render: (_: unknown, r: Attachment) => isEngineeringReference(r) ? <Typography.Text type="warning">未经内容扫描</Typography.Text> : '-' },
                    { title: '操作', width: 120, render: (_: unknown, r: Attachment) => (
                      <Space size={0}>
                        <ContentPreviewButton kind="attachment" id={r.id} disabled={!r.oss_object_id} />
                        <Button type="link" size="small" icon={<DownloadOutlined />} disabled={!r.oss_object_id} loading={attachmentDownloadMutation.isPending} onClick={() => downloadAttachment(r)} title="下载" />
                      </Space>
                    )},
                  ]}
                  expandable={{
                    expandedRowRender: (r: Attachment) => (
                      <div className="evidence-grid">
                        <div><Typography.Text strong>提取文本</Typography.Text><pre className="json-block">{r.extracted_text || '-'}</pre></div>
                        <div><Typography.Text strong>提取 JSON</Typography.Text><JsonBlock value={r.extracted_json} /></div>
                      </div>
                    ),
                  }}
                />
              </div>
            ),
          },
          {
            key: 'parse',
            label: 'AI 解析对比',
            children: (
              <div className="drawer-stack">
                <Table<ParseResult>
                  size="small" rowKey="id" dataSource={context.parse_results} pagination={false}
                  columns={[
                    { title: 'ID', dataIndex: 'id', width: 70 },
                    { title: '解析器', dataIndex: 'parser_type', width: 100 },
                    { title: '意图', dataIndex: 'intent_type', width: 120, render: (v?: string) => v || '-' },
                    { title: '置信度', dataIndex: 'confidence_score', width: 90, render: numberText },
                    { title: '应用状态', dataIndex: 'apply_status', width: 120, render: (v: string) => <StatusTag value={v} /> },
                    { title: '缺失字段', dataIndex: 'missing_fields', render: (v: unknown) => <JsonBlock value={v as JsonRecord} /> },
                    {
                      title: '操作', width: 190,
                      render: (_: unknown, r: ParseResult) => {
                        const handled = Boolean(r.apply_status && r.apply_status !== 'pending');
                        return (
                          <Space size={0}>
                            <Button type="link" size="small" disabled={handled || r.accepted} onClick={() => onApplyParse(r.id)}>采纳</Button>
                            <Button type="link" size="small" disabled={handled || r.accepted} onClick={() => onPartialParse(r)}>部分采纳</Button>
                            <Button type="link" size="small" danger disabled={handled} onClick={() => onRejectParse(r.id)}>拒绝</Button>
                          </Space>
                        );
                      },
                    },
                  ]}
                  expandable={{
                    expandedRowRender: (r: ParseResult) => (
                      <div className="evidence-grid">
                        <div><Typography.Text strong>提取字段</Typography.Text><JsonBlock value={r.extracted_fields} /></div>
                        <div><Typography.Text strong>提取明细</Typography.Text><JsonBlock value={r.extracted_items} /></div>
                        <div><Typography.Text strong>证据</Typography.Text><JsonBlock value={r.evidence} /></div>
                      </div>
                    ),
                  }}
                />
                <div style={{ marginTop: 12 }}>
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <div>
                      <Typography.Text strong>缺失字段：</Typography.Text>
                      <Space wrap size={[4, 4]}>
                        {Object.keys(context.ticket.missing_fields || {}).length > 0
                          ? Object.keys(context.ticket.missing_fields!).map((f) => <Tag color="orange" key={f}>{f}</Tag>)
                          : <Typography.Text type="secondary">-</Typography.Text>}
                      </Space>
                    </div>
                    <div>
                      <Typography.Text strong>冲突字段：</Typography.Text>
                      <Space wrap size={[4, 4]}>
                        {Object.keys(context.ticket.conflict_fields || {}).length > 0
                          ? Object.keys(context.ticket.conflict_fields!).map((f) => <Tag color="red" key={f}>{f}</Tag>)
                          : <Typography.Text type="secondary">-</Typography.Text>}
                      </Space>
                    </div>
                  </Space>
                </div>
                {fieldAudits.length > 0 && (
                  <>
                    <Typography.Title level={5}>字段变更记录</Typography.Title>
                    <Table<FieldAuditLog>
                      size="small" rowKey="id" dataSource={fieldAudits} pagination={false}
                      columns={[
                        { title: '字段', dataIndex: 'field_name', width: 130 },
                        { title: '明细ID', dataIndex: 'ticket_item_id', width: 70, render: (v?: number) => v || '-' },
                        { title: '旧值', dataIndex: 'old_value', ellipsis: true, render: (v?: string) => v || '-' },
                        { title: '新值', dataIndex: 'new_value', ellipsis: true, render: (v?: string) => v || '-' },
                        { title: '来源', dataIndex: 'source_type', width: 80 },
                        { title: '时间', dataIndex: 'created_at', width: 150, render: formatTime },
                      ]}
                    />
                  </>
                )}
              </div>
            ),
          },
          {
            key: 'replies',
            label: `回复记录 (${context.reply_records?.length ?? 0})`,
            children: (
              <div className="drawer-stack">
                {context.reply_records?.length ? (
                  <Table<ReplyRecord>
                    size="small" rowKey="id"
                    dataSource={context.reply_records}
                    pagination={false}
                    columns={[
                      { title: '类型', dataIndex: 'reply_type', width: 100 },
                      { title: '轮次', dataIndex: 'followup_round', width: 60 },
                      { title: '主题', dataIndex: 'subject', ellipsis: true, render: (v?: string) => v || '-' },
                      { title: '收件人', dataIndex: 'to_addresses', ellipsis: true },
                      { title: '审核状态', dataIndex: 'review_status', width: 100, render: (v: string) => <StatusTag value={v} kind="review" /> },
                      { title: '发送状态', dataIndex: 'send_status', width: 100, render: (v: string) => <StatusTag value={v} /> },
                      { title: '创建时间', dataIndex: 'created_at', width: 170, render: formatTime },
                    ]}
                    expandable={{
                      expandedRowRender: (record: ReplyRecord) => (
                        <div className="evidence-grid">
                          <div><Typography.Text strong>草稿内容</Typography.Text><pre className="json-block">{record.draft_body || '-'}</pre></div>
                          <div><Typography.Text strong>最终内容</Typography.Text><pre className="json-block">{record.final_body || '-'}</pre></div>
                          <div><Typography.Text strong>错误信息</Typography.Text><pre className="json-block">{record.error_message || '-'}</pre></div>
                        </div>
                      ),
                    }}
                  />
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无回复记录" />
                )}
              </div>
            ),
          },
          {
            key: 'ticket',
            label: '工单详情',
            children: (
              <div className="drawer-stack">
                <Descriptions column={2} size="small" bordered>
                  <Descriptions.Item label="客户代码">{context.ticket.customer_code || '-'}</Descriptions.Item>
                  <Descriptions.Item label="客户名称">{context.ticket.customer_name || '-'}</Descriptions.Item>
                  <Descriptions.Item label="客户范围">{context.ticket.customer_scope === 'overseas' ? '海外' : context.ticket.customer_scope === 'domestic' ? '国内' : '待确认'}</Descriptions.Item>
                  <Descriptions.Item label="收费状态">{context.ticket.charge_status || '待确认'}</Descriptions.Item>
                  <Descriptions.Item label="客户方邮寄联系人">{context.ticket.contact_person || '-'}</Descriptions.Item>
                  <Descriptions.Item label="客户方邮寄联系方式">{context.ticket.contact_phone || '-'}</Descriptions.Item>
                  <Descriptions.Item label="客户邮箱">{context.ticket.contact_email || '-'}</Descriptions.Item>
                  <Descriptions.Item label="政策解析状态">{context.ticket.policy_resolution_status || 'pending'}</Descriptions.Item>
                  <Descriptions.Item label="客户方邮寄地址" span={2}>{context.ticket.mailing_address || '-'}</Descriptions.Item>
                </Descriptions>
                <Typography.Title level={5}>维修明细</Typography.Title>
                <Table<TicketLine>
                  size="small" rowKey="id" dataSource={context.items} pagination={false}
                  columns={[
                    { title: '行', dataIndex: 'line_no', width: 48 },
                    { title: 'SN', dataIndex: 'sn', width: 140, render: (v?: string) => v || '-' },
                    { title: 'SAP 物料代码', dataIndex: 'material_code', ellipsis: true, render: (v?: string) => v || '-' },
                    { title: 'SAP 物料名称', dataIndex: 'material_name', ellipsis: true, render: (v?: string) => v || '-' },
                    { title: '板卡型号', dataIndex: 'board_code', ellipsis: true, render: (v?: string) => v || '-' },
                    { title: '板卡名称', dataIndex: 'board_name', ellipsis: true, render: (v?: string) => v || '-' },
                    { title: '寄回地点', dataIndex: 'return_location', width: 90, render: (v?: string) => v === 'beijing' ? '北京' : v === 'tianjin' ? '天津' : '-' },
                    { title: '地址匹配', dataIndex: 'return_route_status', width: 100, render: (v?: string) => <StatusTag value={v || 'pending'} kind="route" /> },
                    { title: '维修寄回地址', dataIndex: 'return_address', ellipsis: true, render: (v?: string) => v || '-' },
                    { title: '数量', dataIndex: 'quantity', width: 60 },
                    { title: '故障描述', dataIndex: 'failure_description', ellipsis: true, render: (v?: string) => compactText(v) },
                    {
                      title: '操作',
                      width: 150,
                      render: (_: unknown, r: TicketLine) => (
                        <Space size={0}>
                          <Button type="link" size="small" onClick={() => onEditItem(r)}>编辑</Button>
                          <Button type="link" size="small" onClick={() => onSelectRoute(r)}>选寄回地</Button>
                        </Space>
                      ),
                    },
                  ]}
                />
                <Typography.Text type="secondary">
                  {buildSuggestions(detail).map((item) => <span key={item}>• {item} </span>)}
                </Typography.Text>
              </div>
            ),
          },
        ]}
      />
      {/* Action buttons */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, paddingTop: 8, borderTop: '1px solid #f0f0f0' }}>
        <Button icon={<EditOutlined />} onClick={onEditFields}>字段修正</Button>
        <Button icon={<FileAddOutlined />} onClick={() => onEditItem()}>新增明细</Button>
        <Button icon={<CheckCircleOutlined />} loading={validateLoading} onClick={onValidateSn}>SN 校验</Button>
        <Button loading={resolvePolicyLoading} onClick={onResolvePolicy}>自动解析政策</Button>
        <Button onClick={onOverridePolicy}>人工确认政策</Button>
        <Button loading={resolveRoutesLoading} onClick={onResolveRoutes}>匹配寄回地址</Button>
        <Button icon={<SyncOutlined />} loading={reparseLoading} onClick={onReparse}>重新解析</Button>
        <Button icon={<MailOutlined />} loading={draftLoading} onClick={onDraftReply}>生成追问</Button>
        <Button type="primary" disabled={detail.task.status === 'resolved' || detail.task.status === 'closed'} onClick={onResolve}>完成任务</Button>
      </div>
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
  if (!detail.ticket_context) {
    return ['查看邮件正文、附件和线程上下文后，完成业务定类并记录人工处理结论。'];
  }
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
