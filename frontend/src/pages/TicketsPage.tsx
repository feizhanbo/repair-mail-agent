import {
  CheckCircleOutlined,
  DownloadOutlined,
  EditOutlined,
  FileAddOutlined,
  LeftOutlined,
  MailOutlined,
  RightOutlined,
  SearchOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Button,
  Checkbox,
  DatePicker,
  Descriptions,
  Drawer,
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
import { useMemo, useState, type Key } from 'react';
import { useSearchParams } from 'react-router-dom';
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
import { useAuthStore } from '../stores/authStore';
import type {
  Attachment,
  EmailItem,
  FieldAuditLog,
  ManualTask,
  ParseResult,
  ReplyRecord,
  SnValidationResult,
  StatusLog,
  Ticket,
  TicketDetail,
  TicketLine,
} from '../types/api';
import { ARCHIVE_DOWNLOAD_WARNING, attachmentTypeLabel, isEngineeringReference } from '../utils/attachments';
import { filtersWithDateRange } from '../utils/filters';
import { compactText, formatFileSizeKb, formatTime, numberText } from '../utils/format';
import { saveBlob } from '../utils/download';
import { hasAnyRole } from '../utils/roles';
import { ticketStatusLabels } from '../utils/status';
type TicketFilters = {
  ticket_no?: string;
  customer?: string;
  contact?: string;
  sn?: string;
  status_code?: string;
  date_range?: unknown;
};

type TransitionForm = {
  to_status_code: string;
  trigger_event: string;
  reason?: string;
};

type SapReconcileForm = {
  outcome: 'accepted' | 'not_inserted';
  call_id?: string;
  reason: string;
};

type RmaManualPolicyForm = {
  reason: string;
  confirm_policy_values: true;
  confirm_template_thread_and_archive: true;
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

export default function TicketsPage() {
  const [searchParams] = useSearchParams();
  const [filters, setFilters] = useState<Record<string, unknown>>({});
  const [page, setPage] = useState(1);
  const [selectedTicketKeys, setSelectedTicketKeys] = useState<Key[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(() => {
    const value = Number(searchParams.get('ticket_id'));
    return Number.isInteger(value) && value > 0 ? value : null;
  });
  const [transitionOpen, setTransitionOpen] = useState(false);
  const [fieldOpen, setFieldOpen] = useState(false);
  const [itemOpen, setItemOpen] = useState(false);
  const [editingItem, setEditingItem] = useState<TicketLine | null>(null);
  const [partialParse, setPartialParse] = useState<ParseResult | null>(null);
  const [sapReconcileLineId, setSapReconcileLineId] = useState<number | null>(null);
  const [rmaManualPolicyOpen, setRmaManualPolicyOpen] = useState(false);
  const [policyOverrideOpen, setPolicyOverrideOpen] = useState(false);
  const [returnRouteItem, setReturnRouteItem] = useState<TicketLine | null>(null);
  const [filterForm] = Form.useForm<TicketFilters>();
  const queryClient = useQueryClient();
  const canTransitionTicket = hasAnyRole(useAuthStore((state) => state.user?.roles), ['admin', 'operator']);
  const handleMutationError = (error: unknown) => message.error(apiErrorMessage(error));
  const confirmAction = (title: string, onOk: () => void) => {
    Modal.confirm({
      title,
      content: '该操作会更新当前工单数据。',
      okText: '确认',
      cancelText: '取消',
      onOk,
    });
  };

  const ticketsQuery = useQuery({
    queryKey: ['tickets', filters, page],
    queryFn: () => api.tickets({ ...filters, page, page_size: 20 }),
  });
  const ticketIds = useMemo(() => ticketsQuery.data?.items.map((t) => t.id) ?? [], [ticketsQuery.data]);
  const currentTicketIndex = ticketIds.indexOf(selectedId ?? -1);
  const hasPrevTicket = currentTicketIndex > 0;
  const hasNextTicket = currentTicketIndex >= 0 && currentTicketIndex < ticketIds.length - 1;
  const goToPrevTicket = () => {
    if (currentTicketIndex > 0) setSelectedId(ticketIds[currentTicketIndex - 1]);
  };
  const goToNextTicket = () => {
    if (currentTicketIndex < ticketIds.length - 1) setSelectedId(ticketIds[currentTicketIndex + 1]);
  };
  const detailQuery = useQuery({
    queryKey: ['ticket-detail', selectedId],
    queryFn: () => api.ticketDetail(selectedId as number),
    enabled: Boolean(selectedId),
  });
  const systemQuery = useQuery({
    queryKey: ['system-info'],
    queryFn: api.systemInfo,
    staleTime: 5 * 60_000,
  });

  const transitionOptions = useMemo(() => {
    const transitions = systemQuery.data?.workflow_transitions ?? [];
    return transitions
      .filter((t) => t.enabled && !['ready_for_export', 'rma_sent', 'closed'].includes(t.to_status_code))
      .map((t) => ({
        to_status_code: t.to_status_code,
        trigger_event: t.trigger_event,
        label: ticketStatusLabels[t.to_status_code] ?? t.to_status_code,
        require_manual: t.require_manual,
      }));
  }, [systemQuery.data]);

  const invalidateDetail = () => {
    void queryClient.invalidateQueries({ queryKey: ['ticket-detail', selectedId] });
    void queryClient.invalidateQueries({ queryKey: ['tickets'] });
  };

  const validateMutation = useMutation({
    mutationFn: (id: number) => api.validateTicketSn(id),
    onSuccess: () => {
      message.success('SN 校验完成');
      invalidateDetail();
    },
    onError: handleMutationError,
  });
  const transitionMutation = useMutation({
    mutationFn: (values: TransitionForm) => api.transitionTicket(selectedId as number, values),
    onSuccess: () => {
      message.success('状态已更新');
      setTransitionOpen(false);
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: ['manual-tasks'] });
    },
    onError: handleMutationError,
  });
  const patchFieldsMutation = useMutation({
    mutationFn: (values: TicketFieldForm) => {
      const ticket = detailQuery.data?.ticket;
      return api.patchTicketFields(selectedId as number, { version: ticket?.version, fields: values, reason: '前端人工修正字段' });
    },
    onSuccess: () => {
      message.success('字段已保存');
      setFieldOpen(false);
      invalidateDetail();
    },
    onError: handleMutationError,
  });
  const patchItemsMutation = useMutation({
    mutationFn: (values: TicketItemForm) => {
      const item = editingItem ? { id: editingItem.id, ...values } : values;
      return api.patchTicketItems(selectedId as number, { items: [item], reason: '前端人工修正明细' });
    },
    onSuccess: () => {
      message.success('明细已保存');
      setItemOpen(false);
      setEditingItem(null);
      invalidateDetail();
    },
    onError: handleMutationError,
  });
  const applyParseMutation = useMutation({
    mutationFn: ({ id, action, selected_fields, selected_item_indices }: { id: number; action: 'apply' | 'partial_apply' | 'reject'; selected_fields?: string[]; selected_item_indices?: number[] }) => api.applyParseResult(id, {
      action,
      selected_fields,
      selected_item_indices,
      reason: action === 'reject' ? '前端人工拒绝解析候选' : '前端人工采纳解析候选',
    }),
    onSuccess: () => {
      message.success('解析候选状态已更新');
      setPartialParse(null);
      invalidateDetail();
    },
    onError: handleMutationError,
  });
  const draftReplyMutation = useMutation({
    mutationFn: () => {
      const detail = detailQuery.data as TicketDetail;
      return api.draftReply(detail.ticket.id, {
        reply_type: 'followup',
        related_email_id: detail.source_email?.id,
        language: 'zh-CN',
        missing_fields: detail.ticket.missing_fields ?? undefined,
      });
    },
    onSuccess: () => {
      message.success('追问草稿已生成');
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: ['replies'] });
    },
    onError: handleMutationError,
  });
  const validateExportMutation = useMutation({
    mutationFn: (id: number) => api.validateTicketExport(id),
    onSuccess: () => {
      message.success('完整安全校验已完成');
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: ['manual-tasks'] });
    },
    onError: handleMutationError,
  });
  const confirmDeviceReceivedMutation = useMutation({
    mutationFn: (id: number) => api.confirmDeviceReceived(id, {
      idempotency_key: `manual-${id}-${Date.now()}`,
      note: '操作员在工单中心确认公司已收到待修设备。',
    }),
    onSuccess: (result) => {
      message.success('公司收货事实已记录；不会发送邮件或改变 RMA 签发状态');
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: ['replies'] });
      void queryClient.invalidateQueries({ queryKey: ['manual-tasks'] });
    },
    onError: handleMutationError,
  });
  const retrySapMutation = useMutation({
    mutationFn: (id: number) => api.retrySapExport(id),
    onSuccess: () => {
      message.success('SAP 提交重试已进入后台队列');
      invalidateDetail();
    },
    onError: handleMutationError,
  });
  const pollSapMutation = useMutation({
    mutationFn: (id: number) => api.pollSapExport(id),
    onSuccess: () => {
      message.success('已重新查询 SAP RMA 回填状态');
      invalidateDetail();
    },
    onError: handleMutationError,
  });
  const confirmLateSapMutation = useMutation({
    mutationFn: (id: number) => api.confirmLateSapResult(id),
    onSuccess: () => {
      message.success('迟到的 SAP 回填结果已确认');
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: ['manual-tasks'] });
    },
    onError: handleMutationError,
  });
  const reconcileSapMutation = useMutation({
    mutationFn: (values: SapReconcileForm) => api.reconcileSapSubmission(
      selectedId as number,
      sapReconcileLineId as number,
      values,
    ),
    onSuccess: () => {
      message.success('SAP 不确定提交结果已完成对账');
      setSapReconcileLineId(null);
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: ['manual-tasks'] });
    },
    onError: handleMutationError,
  });
  const retryRmaMutation = useMutation({
    mutationFn: (id: number) => api.retryRmaSend(id),
    onSuccess: () => {
      message.success('RMA 签发恢复已受理；已发送邮件只会重试归档，不会重复发送');
      invalidateDetail();
    },
    onError: handleMutationError,
  });
  const approveRmaManualPolicyMutation = useMutation({
    mutationFn: (values: RmaManualPolicyForm) => api.approveRmaManualPolicy(
      selectedId as number,
      values,
    ),
    onSuccess: () => {
      message.success('特殊政策已记录，RMA 模板草稿生成任务已进入队列；不会自动发送');
      setRmaManualPolicyOpen(false);
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: ['manual-tasks'] });
    },
    onError: handleMutationError,
  });
  const resolvePolicyMutation = useMutation({
    mutationFn: () => api.resolveTicketPolicy(selectedId as number),
    onSuccess: () => {
      message.success('客户范围与服务政策已重新解析');
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: ['manual-tasks'] });
    },
    onError: handleMutationError,
  });
  const overridePolicyMutation = useMutation({
    mutationFn: (values: PolicyOverrideForm) => api.overrideTicketPolicy(
      selectedId as number,
      values,
    ),
    onSuccess: () => {
      message.success('仅当前工单的客户范围与收费状态已人工确认');
      setPolicyOverrideOpen(false);
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: ['manual-tasks'] });
    },
    onError: handleMutationError,
  });
  const resolveReturnRoutesMutation = useMutation({
    mutationFn: () => api.resolveReturnRoutes(selectedId as number),
    onSuccess: () => {
      message.success('维修寄回地址已重新匹配');
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: ['manual-tasks'] });
    },
    onError: handleMutationError,
  });
  const selectReturnRouteMutation = useMutation({
    mutationFn: (values: ReturnRouteForm) => api.selectReturnRoute(
      selectedId as number,
      returnRouteItem?.id as number,
      values,
    ),
    onSuccess: () => {
      message.success('当前明细的维修寄回地点已人工确认并快照');
      setReturnRouteItem(null);
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: ['manual-tasks'] });
    },
    onError: handleMutationError,
  });
  const exportMutation = useMutation({
    mutationFn: () => api.exportSelectedTickets(selectedTicketKeys.map(Number)),
    onSuccess: (blob) => saveBlob(blob, 'tickets-selected-export.xlsx'),
    onError: handleMutationError,
  });

  const openItemEditor = (item?: TicketLine) => {
    setEditingItem(item ?? null);
    setItemOpen(true);
  };

  const columns: ColumnsType<Ticket> = [
    { title: '工单号', dataIndex: 'ticket_no', width: 170 },
    { title: '状态', dataIndex: 'current_status_code', width: 130, render: (value: string) => <StatusTag value={value} kind="ticket" /> },
    {
      title: '原因/摘要',
      width: 180,
      ellipsis: true,
      render: (_: unknown, record: Ticket) => {
        if (record.current_status_code === 'manual_review') {
          return <Typography.Text type="warning">需人工复核</Typography.Text>;
        }
        if (record.current_status_code === 'error') {
          return (
            <Typography.Text type="danger" ellipsis={{ tooltip: record.problem_description }}>
              {record.problem_description?.slice(0, 40) || '系统异常'}
            </Typography.Text>
          );
        }
        return '-';
      },
    },
    { title: '客户', dataIndex: 'customer_name', ellipsis: true, render: (value?: string) => value || '-' },
    { title: '联系人', dataIndex: 'contact_email', ellipsis: true, width: 220, render: (value?: string) => value || '-' },
    { title: '追问', dataIndex: 'followup_count', width: 80, render: (_, record) => `${record.followup_count}/${record.max_followup_count}` },
    { title: '置信度', dataIndex: 'confidence_score', width: 90, render: numberText },
    { title: '更新时间', dataIndex: 'updated_at', width: 160, render: formatTime },
    { title: '操作', width: 90, render: (_, record) => <Button type="link" size="small" onClick={() => setSelectedId(record.id)}>详情</Button> },
  ];

  return (
    <div className="page-stack">
      <PageTitle
        title="工单中心"
        extra={(
          <Button icon={<DownloadOutlined />} disabled={!selectedTicketKeys.length} loading={exportMutation.isPending} onClick={() => exportMutation.mutate()}>
            导出已选{selectedTicketKeys.length ? `(${selectedTicketKeys.length})` : ''}
          </Button>
        )}
      />
      <SectionPanel>
        <Form<TicketFilters>
          form={filterForm}
          layout="inline"
          className="filter-bar"
          onFinish={(values) => {
            setPage(1);
            setSelectedId(null);
            setSelectedTicketKeys([]);
            setFilters(filtersWithDateRange(values, 'date_range', 'request_date_start', 'request_date_end'));
          }}
        >
          <Form.Item name="ticket_no">
            <Input allowClear prefix={<SearchOutlined />} placeholder="工单号" />
          </Form.Item>
          <Form.Item name="customer">
            <Input allowClear placeholder="客户" />
          </Form.Item>
          <Form.Item name="contact">
            <Input allowClear placeholder="联系人" />
          </Form.Item>
          <Form.Item name="sn">
            <Input allowClear placeholder="SN" />
          </Form.Item>
          <Form.Item name="status_code">
            <Select
              allowClear
              placeholder="工单状态"
              style={{ width: 160 }}
              options={Object.entries(ticketStatusLabels).map(([value, label]) => ({ value, label }))}
            />
          </Form.Item>
          <Form.Item name="date_range">
            <DatePicker.RangePicker allowClear />
          </Form.Item>
          <Space>
            <Button htmlType="submit" type="primary">筛选</Button>
            <Button
              onClick={() => {
                filterForm.resetFields();
                setPage(1);
                setSelectedId(null);
                setSelectedTicketKeys([]);
                setFilters({});
              }}
            >
              重置
            </Button>
          </Space>
        </Form>
        <Table<Ticket>
          rowKey="id"
          columns={columns}
          dataSource={ticketsQuery.data?.items ?? []}
          loading={ticketsQuery.isFetching}
          locale={{
            emptyText: ticketsQuery.isError
              ? <ErrorResult message={apiErrorMessage(ticketsQuery.error)} onRetry={() => ticketsQuery.refetch()} />
              : '暂无工单'
          }}
          rowSelection={{ selectedRowKeys: selectedTicketKeys, onChange: setSelectedTicketKeys }}
          pagination={{
            current: page,
            pageSize: 20,
            total: ticketsQuery.data?.total ?? 0,
            onChange: (nextPage) => {
              setPage(nextPage);
              setSelectedTicketKeys([]);
            },
            showSizeChanger: false,
          }}
        />
      </SectionPanel>
      <Drawer
        width="min(1280px, 96vw)"
        title="工单详情工作台"
        open={Boolean(selectedId)}
        onClose={() => {
          setSelectedId(null);
          setEditingItem(null);
        }}
      >
        {detailQuery.data ? (
          <TicketDetailView
            detail={detailQuery.data}
            onApplyParse={(id) => confirmAction('确认采纳该解析候选？', () => applyParseMutation.mutate({ id, action: 'apply' }))}
            onPartialParse={setPartialParse}
            onRejectParse={(id) => confirmAction('确认拒绝该解析候选？', () => applyParseMutation.mutate({ id, action: 'reject' }))}
            onEditItem={openItemEditor}
            onAddItem={() => openItemEditor()}
            onEditFields={() => setFieldOpen(true)}
            onValidateSn={() => confirmAction('确认执行 SN 校验？', () => validateMutation.mutate(detailQuery.data.ticket.id))}
            onDraftReply={() => confirmAction('确认生成追问草稿？', () => draftReplyMutation.mutate())}
            onValidateExport={() => confirmAction('确认执行完整可导出安全校验？', () => validateExportMutation.mutate(detailQuery.data.ticket.id))}
            onRetrySap={() => confirmAction('确认使用原提交键和原快照重试 SAP 提交？', () => retrySapMutation.mutate(detailQuery.data.ticket.id))}
            onPollSap={() => pollSapMutation.mutate(detailQuery.data.ticket.id)}
            onConfirmLateSap={() => confirmAction('确认接受迟到的 SAP RMA 回填结果？', () => confirmLateSapMutation.mutate(detailQuery.data.ticket.id))}
            onReconcileSap={setSapReconcileLineId}
            onRetryRma={() => confirmAction('确认继续RMA签发？系统会优先恢复归档，只有明确未发送时才允许重新发送。', () => retryRmaMutation.mutate(detailQuery.data.ticket.id))}
            onApproveRmaManualPolicy={() => setRmaManualPolicyOpen(true)}
            onResolvePolicy={() => resolvePolicyMutation.mutate()}
            onOverridePolicy={() => setPolicyOverrideOpen(true)}
            onResolveReturnRoutes={() => resolveReturnRoutesMutation.mutate()}
            onSelectReturnRoute={setReturnRouteItem}
            onConfirmDeviceReceived={() => confirmAction('确认公司已经收到客户寄来的待修设备及纸质 RMA 授权单？', () => confirmDeviceReceivedMutation.mutate(detailQuery.data.ticket.id))}
            onTransition={() => setTransitionOpen(true)}
            hasPrevTicket={hasPrevTicket}
            hasNextTicket={hasNextTicket}
            onGoPrev={goToPrevTicket}
            onGoNext={goToNextTicket}
            canTransitionTicket={canTransitionTicket}
            validateLoading={validateMutation.isPending}
            draftLoading={draftReplyMutation.isPending}
            validateExportLoading={validateExportMutation.isPending}
            retrySapLoading={retrySapMutation.isPending}
            pollSapLoading={pollSapMutation.isPending}
            confirmLateSapLoading={confirmLateSapMutation.isPending}
            retryRmaLoading={retryRmaMutation.isPending}
            approveRmaManualPolicyLoading={approveRmaManualPolicyMutation.isPending}
            resolvePolicyLoading={resolvePolicyMutation.isPending}
            resolveReturnRoutesLoading={resolveReturnRoutesMutation.isPending}
            confirmDeviceLoading={confirmDeviceReceivedMutation.isPending}
          />
        ) : detailQuery.isFetching ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="正在加载工单详情" />
        ) : detailQuery.isError ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="工单详情加载失败" />
        ) : null}
      </Drawer>
      <Modal title="编辑工单字段" open={fieldOpen} onCancel={() => setFieldOpen(false)} footer={null} destroyOnClose>
        {detailQuery.data ? (
          <TicketFieldEditor
            initialValues={{
              customer_code: detailQuery.data.ticket.customer_code ?? undefined,
              customer_name: detailQuery.data.ticket.customer_name ?? undefined,
              contact_person: detailQuery.data.ticket.contact_person ?? undefined,
              contact_phone: detailQuery.data.ticket.contact_phone ?? undefined,
              contact_email: detailQuery.data.ticket.contact_email ?? undefined,
              request_date: detailQuery.data.ticket.request_date ?? undefined,
              mailing_address: detailQuery.data.ticket.mailing_address ?? undefined,
              problem_description: detailQuery.data.ticket.problem_description ?? undefined,
              accessories: detailQuery.data.ticket.accessories ?? undefined,
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
      {canTransitionTicket ? (
        <Modal title="状态流转" open={transitionOpen} onCancel={() => setTransitionOpen(false)} footer={null} destroyOnClose>
          <Form<TransitionForm> layout="vertical" onFinish={(values) => transitionMutation.mutate(values)}>
            <Form.Item label="目标状态" name="to_status_code" rules={[{ required: true }]}>
              <Select options={transitionOptions.map((item) => ({ value: item.to_status_code, label: item.label }))} />
            </Form.Item>
            <Form.Item label="触发事件" name="trigger_event" rules={[{ required: true }]}>
              <Select options={transitionOptions.map((item) => ({ value: item.trigger_event, label: item.trigger_event }))} />
            </Form.Item>
            <Form.Item label="原因" name="reason">
              <Input.TextArea rows={3} />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={transitionMutation.isPending}>
              提交
            </Button>
          </Form>
        </Modal>
      ) : null}
      <Modal
        title="人工确认当前工单政策"
        open={policyOverrideOpen}
        onCancel={() => setPolicyOverrideOpen(false)}
        footer={null}
        destroyOnClose
      >
        <Typography.Paragraph type="secondary">
          此操作只修改当前工单快照，不会反向修改客户服务政策主数据。
        </Typography.Paragraph>
        <Form<PolicyOverrideForm>
          layout="vertical"
          initialValues={{
            charge_status: (
              detailQuery.data?.ticket.charge_status === 'free'
              || detailQuery.data?.ticket.charge_status === 'annual_contract'
              || detailQuery.data?.ticket.charge_status === 'chargeable'
            ) ? detailQuery.data.ticket.charge_status : 'chargeable',
            customer_scope: detailQuery.data?.ticket.customer_scope || 'domestic',
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
            保存当前工单政策快照
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
          onFinish={(values) => selectReturnRouteMutation.mutate(values)}
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
          <Button type="primary" htmlType="submit" loading={selectReturnRouteMutation.isPending}>
            保存地点与地址快照
          </Button>
        </Form>
      </Modal>
      <Modal
        title="SAP 提交结果人工对账"
        open={sapReconcileLineId !== null}
        onCancel={() => setSapReconcileLineId(null)}
        footer={null}
        destroyOnClose
      >
        <Typography.Paragraph type="warning">
          仅在远端人工核对后操作。确认已插入时必须填写 SAP 生成的 CallID；确认未插入后系统才允许安全重试。
        </Typography.Paragraph>
        <Form<SapReconcileForm>
          layout="vertical"
          initialValues={{ outcome: 'accepted' }}
          onFinish={(values) => reconcileSapMutation.mutate(values)}
        >
          <Form.Item label="核对结果" name="outcome" rules={[{ required: true }]}>
            <Select options={[
              { value: 'accepted', label: '远端已插入，绑定 CallID' },
              { value: 'not_inserted', label: '远端确认未插入，允许重试' },
            ]} />
          </Form.Item>
          <Form.Item
            label="CallID"
            name="call_id"
            dependencies={['outcome']}
            rules={[
              ({ getFieldValue }) => ({
                validator: (_, value) => (
                  getFieldValue('outcome') !== 'accepted' || String(value || '').trim()
                    ? Promise.resolve()
                    : Promise.reject(new Error('远端已插入时必须填写 CallID'))
                ),
              }),
            ]}
          >
            <Input maxLength={191} />
          </Form.Item>
          <Form.Item label="核对依据/原因" name="reason" rules={[{ required: true, min: 3, max: 500 }]}>
            <Input.TextArea rows={4} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={reconcileSapMutation.isPending}>
            提交对账结果
          </Button>
        </Form>
      </Modal>
      <Modal
        title="特殊 RMA 政策人工确认"
        open={rmaManualPolicyOpen}
        onCancel={() => setRmaManualPolicyOpen(false)}
        footer={null}
        destroyOnClose
      >
        <Typography.Paragraph type="warning">
          此操作只生成“待人工审核”的模板草稿，不会自动发送。匿名客户因固定 PDF 版式不适用，系统会继续阻止并要求单独制作受控附件。
        </Typography.Paragraph>
        <Form<RmaManualPolicyForm>
          layout="vertical"
          onFinish={(values) => approveRmaManualPolicyMutation.mutate(values)}
        >
          <Form.Item label="人工确认依据" name="reason" rules={[{ required: true, min: 3, max: 500 }]}>
            <Input.TextArea rows={4} />
          </Form.Item>
          <Form.Item
            name="confirm_policy_values"
            valuePropName="checked"
            rules={[{
              validator: (_, value) => value
                ? Promise.resolve()
                : Promise.reject(new Error('必须确认价格、币种和税率')),
            }]}
          >
            <Checkbox>我已核对价格、币种、税率及客户政策快照</Checkbox>
          </Form.Item>
          <Form.Item
            name="confirm_template_thread_and_archive"
            valuePropName="checked"
            rules={[{
              validator: (_, value) => value
                ? Promise.resolve()
                : Promise.reject(new Error('必须确认模板、原线程和归档要求')),
            }]}
          >
            <Checkbox>我确认最终邮件必须使用模板、在原线程发送并保存 Message-ID 与归档证据</Checkbox>
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={approveRmaManualPolicyMutation.isPending}>
            确认并生成待审草稿
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

function TicketDetailView({
  detail,
  onApplyParse,
  onPartialParse,
  onRejectParse,
  onEditItem,
  onAddItem,
  onEditFields,
  onValidateSn,
  onDraftReply,
  onValidateExport,
  onRetrySap,
  onPollSap,
  onConfirmLateSap,
  onReconcileSap,
  onRetryRma,
  onApproveRmaManualPolicy,
  onResolvePolicy,
  onOverridePolicy,
  onResolveReturnRoutes,
  onSelectReturnRoute,
  onConfirmDeviceReceived,
  onTransition,
  hasPrevTicket,
  hasNextTicket,
  onGoPrev,
  onGoNext,
  canTransitionTicket,
  validateLoading,
  draftLoading,
  validateExportLoading,
  retrySapLoading,
  pollSapLoading,
  confirmLateSapLoading,
  retryRmaLoading,
  approveRmaManualPolicyLoading,
  resolvePolicyLoading,
  resolveReturnRoutesLoading,
  confirmDeviceLoading,
}: {
  detail: TicketDetail;
  onApplyParse: (id: number) => void;
  onPartialParse: (record: ParseResult) => void;
  onRejectParse: (id: number) => void;
  onEditItem: (item: TicketLine) => void;
  onAddItem: () => void;
  onEditFields: () => void;
  onValidateSn: () => void;
  onDraftReply: () => void;
  onValidateExport: () => void;
  onRetrySap: () => void;
  onPollSap: () => void;
  onConfirmLateSap: () => void;
  onReconcileSap: (lineId: number) => void;
  onRetryRma: () => void;
  onApproveRmaManualPolicy: () => void;
  onResolvePolicy: () => void;
  onOverridePolicy: () => void;
  onResolveReturnRoutes: () => void;
  onSelectReturnRoute: (item: TicketLine) => void;
  onConfirmDeviceReceived: () => void;
  onTransition: () => void;
  hasPrevTicket: boolean;
  hasNextTicket: boolean;
  onGoPrev: () => void;
  onGoNext: () => void;
  canTransitionTicket: boolean;
  validateLoading: boolean;
  draftLoading: boolean;
  validateExportLoading: boolean;
  retrySapLoading: boolean;
  pollSapLoading: boolean;
  confirmLateSapLoading: boolean;
  retryRmaLoading: boolean;
  approveRmaManualPolicyLoading: boolean;
  resolvePolicyLoading: boolean;
  resolveReturnRoutesLoading: boolean;
  confirmDeviceLoading: boolean;
}) {
  const timelineEmails = detail.email_timeline.length > 0 ? detail.email_timeline : detail.source_email ? [detail.source_email] : [];
  const fieldAudits = detail.field_evidence?.field_audits ?? [];
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

  return (
    <div className="ticket-detail-layout">
      <div className="ticket-summary-bar">
        <Space size="large">
          <CopyableField value={detail.ticket.ticket_no} style={{ fontWeight: 600 }} />
          <StatusTag value={detail.ticket.current_status_code} kind="ticket" />
          <Typography.Text>{detail.ticket.customer_name || '-'}</Typography.Text>
          <Tag color="blue">v{detail.ticket.version}</Tag>
        </Space>
        <Space>
          <Button icon={<LeftOutlined />} disabled={!hasPrevTicket} onClick={onGoPrev}>上一条</Button>
          <Button icon={<RightOutlined />} disabled={!hasNextTicket} onClick={onGoNext}>下一条</Button>
        </Space>
      </div>
      <Tabs
        items={[
          {
            key: 'items',
            label: `维修明细(${detail.items.length})`,
            children: (
              <div className="drawer-stack">
                <Typography.Title level={5}>SAP 物料与客户政策</Typography.Title>
                <Descriptions size="small" column={3} bordered>
                  <Descriptions.Item label="客户代码">{detail.ticket.customer_code || '-'}</Descriptions.Item>
                  <Descriptions.Item label="客户名称">{detail.ticket.customer_name || '-'}</Descriptions.Item>
                  <Descriptions.Item label="客户范围">
                    {detail.ticket.customer_scope === 'overseas' ? '海外' : detail.ticket.customer_scope === 'domestic' ? '国内' : '待确认'}
                  </Descriptions.Item>
                  <Descriptions.Item label="收费状态">{detail.ticket.charge_status || '待确认'}</Descriptions.Item>
                  <Descriptions.Item label="政策解析状态">{detail.ticket.policy_resolution_status || 'pending'}</Descriptions.Item>
                  <Descriptions.Item label="SN 校验状态">{detail.ticket.sn_validation_status || 'pending'}</Descriptions.Item>
                </Descriptions>
                <Table<TicketLine>
                  size="small" rowKey="id" dataSource={detail.items} pagination={false}
                  columns={[
                    { title: '行', dataIndex: 'line_no', width: 48 },
                    { title: 'SN', dataIndex: 'sn', width: 140, render: (v?: string) => <CopyableField value={v || '-'} /> },
                    { title: 'SAP 物料代码', dataIndex: 'material_code', width: 140, render: (v?: string) => v || '-' },
                    { title: 'SAP 物料名称', dataIndex: 'material_name', width: 170, render: (v?: string) => v || '-' },
                    { title: '板卡型号', dataIndex: 'board_code', width: 120, render: (v?: string) => v || '-' },
                    { title: '板卡名称', dataIndex: 'board_name', width: 150, render: (v?: string) => v || '-' },
                    { title: '寄回地点', dataIndex: 'return_location', width: 100, render: (v?: string) => v === 'beijing' ? '北京' : v === 'tianjin' ? '天津' : '-' },
                    { title: '地址匹配', dataIndex: 'return_route_status', width: 110, render: (v?: string) => <StatusTag value={v || 'pending'} kind="route" /> },
                    { title: '判断依据', dataIndex: 'return_route_source', width: 150, render: (v?: string) => v || '-' },
                    { title: '匹配说明', dataIndex: 'return_route_message', ellipsis: true, render: (v?: string) => v || '-' },
                    { title: '数量', dataIndex: 'quantity', width: 60 },
                    { title: '校验', dataIndex: 'validation_status', width: 100, render: (v: string) => <StatusTag value={v} kind="validation" /> },
                    { title: '校验说明', dataIndex: 'validation_message', ellipsis: true, render: (v?: string) => v || '-' },
                    { title: '故障描述', dataIndex: 'failure_description', ellipsis: true, render: (v?: string) => compactText(v) },
                    {
                      title: '操作',
                      width: 150,
                      render: (_: unknown, r: TicketLine) => (
                        <Space size={0}>
                          <Button type="link" size="small" onClick={() => onEditItem(r)}>编辑</Button>
                          <Button type="link" size="small" onClick={() => onSelectReturnRoute(r)}>选寄回地</Button>
                        </Space>
                      ),
                    },
                  ]}
                />
                <Typography.Title level={5}>板卡与维修寄回信息</Typography.Title>
                {detail.items.map((item) => (
                  <Descriptions key={`route-${item.id}`} size="small" column={3} bordered>
                    <Descriptions.Item label={`明细 ${item.line_no} 板卡`}>{item.board_code || '-'} / {item.board_name || '-'}</Descriptions.Item>
                    <Descriptions.Item label="寄回地点">{item.return_location === 'beijing' ? '北京' : item.return_location === 'tianjin' ? '天津' : '-'}</Descriptions.Item>
                    <Descriptions.Item label="匹配状态">{item.return_route_status || 'pending'}</Descriptions.Item>
                    <Descriptions.Item label="维修寄回地址" span={3}>{item.return_address || '-'}</Descriptions.Item>
                    <Descriptions.Item label="维修收货联系人">{item.return_contact || '-'}</Descriptions.Item>
                    <Descriptions.Item label="维修收货电话">{item.return_phone || '-'}</Descriptions.Item>
                    <Descriptions.Item label="邮编">{item.return_postal_code || '-'}</Descriptions.Item>
                  </Descriptions>
                ))}
                <Typography.Title level={5}>客户方邮寄信息</Typography.Title>
                <Descriptions size="small" column={3} bordered>
                  <Descriptions.Item label="客户方邮寄地址" span={3}>{detail.ticket.mailing_address || '-'}</Descriptions.Item>
                  <Descriptions.Item label="客户方邮寄联系人">{detail.ticket.contact_person || '-'}</Descriptions.Item>
                  <Descriptions.Item label="客户方邮寄联系方式">{detail.ticket.contact_phone || '-'}</Descriptions.Item>
                  <Descriptions.Item label="客户邮箱">{detail.ticket.contact_email || '-'}</Descriptions.Item>
                </Descriptions>
                {detail.parse_results.length > 0 && (
                  <>
                    <Typography.Title level={5}>解析候选</Typography.Title>
                    <Table<ParseResult> size="small" rowKey="id" dataSource={detail.parse_results} pagination={false}
                      columns={[
                        { title: 'ID', dataIndex: 'id', width: 70 },
                        { title: '解析器', dataIndex: 'parser_type', width: 100 },
                        { title: '意图', dataIndex: 'intent_type', width: 130, render: (v?: string) => v || '-' },
                        { title: '置信度', dataIndex: 'confidence_score', width: 90, render: numberText },
                        { title: '应用状态', dataIndex: 'apply_status', width: 120, render: (v: string) => <StatusTag value={v} /> },
                        { title: '缺失字段', dataIndex: 'missing_fields', render: (v: unknown) => <JsonBlock value={v as Record<string, unknown>} /> },
                        {
                          title: '操作', width: 190,
                          render: (_: unknown, record: ParseResult) => {
                            const handled = Boolean(record.apply_status && record.apply_status !== 'pending');
                            return (
                              <Space size={0}>
                                <Button type="link" size="small" disabled={handled || record.accepted} onClick={() => onApplyParse(record.id)}>采纳</Button>
                                <Button type="link" size="small" disabled={handled || record.accepted} onClick={() => onPartialParse(record)}>部分采纳</Button>
                                <Button type="link" size="small" danger disabled={handled} onClick={() => onRejectParse(record.id)}>拒绝</Button>
                              </Space>
                            );
                          },
                        },
                      ]}
                      expandable={{
                        expandedRowRender: (record: ParseResult) => (
                          <div className="evidence-grid">
                            <div><Typography.Text strong>提取字段</Typography.Text><JsonBlock value={record.extracted_fields} /></div>
                            <div><Typography.Text strong>提取明细</Typography.Text><JsonBlock value={record.extracted_items} /></div>
                            <div><Typography.Text strong>字段证据</Typography.Text><JsonBlock value={record.evidence} /></div>
                          </div>
                        ),
                      }}
                    />
                  </>
                )}
              </div>
            ),
          },
          {
            key: 'evidence',
            label: '证据对比',
            children: (
              <div className="drawer-stack">
                {/* === P2-1 恢复: Thread 上下文摘要 === */}
                {detail.thread && (
                  <Descriptions size="small" column={4} bordered style={{ marginBottom: 16 }}>
                    <Descriptions.Item label="Thread ID">{detail.thread.id}</Descriptions.Item>
                    <Descriptions.Item label="Thread Key">{detail.thread.thread_key || '-'}</Descriptions.Item>
                    <Descriptions.Item label="邮件数">{detail.thread.email_count ?? '-'}</Descriptions.Item>
                    <Descriptions.Item label="合并置信度">{detail.thread.merge_confidence != null ? numberText(detail.thread.merge_confidence) : '-'}</Descriptions.Item>
                  </Descriptions>
                )}
                {timelineEmails.length > 0 && (
                  <>
                    <Typography.Title level={5}>邮件时间线</Typography.Title>
                    <Timeline items={timelineEmails.map((email: EmailItem) => ({ children: <EmailTimelineItem email={email} /> }))} />
                  </>
                )}
                {detail.attachments.length > 0 && (
                  <>
                    <Typography.Title level={5}>附件</Typography.Title>
                    <Table<Attachment> size="small" rowKey="id" dataSource={detail.attachments} pagination={false}
                      columns={[
                        { title: '文件名', dataIndex: 'file_name', ellipsis: true },
                        { title: '类型', dataIndex: 'content_type', width: 160, render: (v?: string) => v || '-' },
                        { title: '附件类型', width: 170, render: (_: unknown, r: Attachment) => attachmentTypeLabel(r) },
                        { title: '发送时间', dataIndex: 'sent_at', width: 150, render: formatTime },
                        { title: '大小', dataIndex: 'file_size_kb', width: 100, render: (_v: unknown, r: Attachment) => formatFileSizeKb(r.file_size_kb, r.file_size) },
                        { title: '解析状态', dataIndex: 'parse_status', width: 150, render: (v: string, r: Attachment) => <StatusTag value={isEngineeringReference(r) ? 'engineering_reference_stored' : v} kind="parse" /> },
                        { title: '安全提示', width: 140, render: (_: unknown, r: Attachment) => isEngineeringReference(r) ? <Typography.Text type="warning">未经内容扫描</Typography.Text> : '-' },
                        { title: '解析错误', dataIndex: 'parse_error', ellipsis: true, render: (v?: string) => v || '-' },
                        { title: '操作', width: 120, render: (_: unknown, r: Attachment) => (
                          <Space size={0}>
                            <ContentPreviewButton kind="attachment" id={r.id} disabled={!r.oss_object_id} />
                            <Button type="link" size="small" icon={<DownloadOutlined />} disabled={!r.oss_object_id} loading={attachmentDownloadMutation.isPending} onClick={() => downloadAttachment(r)} title="下载" />
                          </Space>
                        )},
                      ]}
                      expandable={{
                        expandedRowRender: (record: Attachment) => (
                          <div className="evidence-grid">
                            <div><Typography.Text strong>提取文本</Typography.Text><pre className="json-block">{record.extracted_text || '-'}</pre></div>
                            <div><Typography.Text strong>提取 JSON</Typography.Text><JsonBlock value={record.extracted_json} /></div>
                          </div>
                        ),
                      }}
                    />
                  </>
                )}
                <div className="two-column-grid">
                  <div><Typography.Title level={5}>缺失字段</Typography.Title><JsonBlock value={detail.ticket.missing_fields} /></div>
                  <div><Typography.Title level={5}>冲突字段</Typography.Title><JsonBlock value={detail.ticket.conflict_fields} /></div>
                </div>
                {fieldAudits.length > 0 && (
                  <>
                    <Typography.Title level={5}>字段变更记录</Typography.Title>
                    <Table<FieldAuditLog> size="small" rowKey="id" dataSource={fieldAudits} pagination={false}
                      columns={[
                        { title: '字段', dataIndex: 'field_name', width: 150 },
                        { title: '明细 ID', dataIndex: 'ticket_item_id', width: 90, render: (v?: number) => v || '-' },
                        { title: '旧值', dataIndex: 'old_value', ellipsis: true, render: (v?: string) => v || '-' },
                        { title: '新值', dataIndex: 'new_value', ellipsis: true, render: (v?: string) => v || '-' },
                        { title: '来源', dataIndex: 'source_type', width: 100 },
                        { title: '原因', dataIndex: 'reason', ellipsis: true, render: (v?: string) => v || '-' },
                        { title: '时间', dataIndex: 'created_at', width: 160, render: formatTime },
                      ]}
                    />
                  </>
                )}
                {/* === P0-1 恢复: SN 校验明细表 === */}
                {detail.sn_validation_results.length > 0 && (
                  <>
                    <Typography.Title level={5}>SN 校验结果</Typography.Title>
                    <Table<SnValidationResult>
                      size="small" rowKey="id"
                      dataSource={detail.sn_validation_results}
                      pagination={false}
                      columns={[
                        { title: 'SN', dataIndex: 'sn', width: 150, render: (v?: string) => <CopyableField value={v || '-'} /> },
                        { title: '结果', dataIndex: 'result_status', width: 100, render: (v: string) => <StatusTag value={v} /> },
                        { title: '存在', dataIndex: 'check_exists', width: 60, render: (v: unknown) => v === true ? '✅' : v === false ? '❌' : '-' },
                        { title: '有效', dataIndex: 'check_valid', width: 60, render: (v: unknown) => v === true ? '✅' : v === false ? '❌' : '-' },
                        { title: '客户匹配', dataIndex: 'check_customer_match', width: 80, render: (v: unknown) => v === true ? '✅' : v === false ? '❌' : '-' },
                        { title: '物料匹配', dataIndex: 'check_material_match', width: 80, render: (v: unknown) => v === true ? '✅' : v === false ? '❌' : '-' },
                        { title: '校验说明', dataIndex: 'result_message', ellipsis: true, render: (v?: string) => v || '-' },
                        { title: '校验时间', dataIndex: 'checked_at', width: 170, render: formatTime },
                      ]}
                    />
                  </>
                )}
                {/* === P0-2 恢复: 安全闸门快照 === */}
                {detail.ticket.sn_validation_snapshot && (
                  <>
                    <Typography.Title level={5}>SN 校验快照</Typography.Title>
                    <JsonBlock value={detail.ticket.sn_validation_snapshot} />
                  </>
                )}
                {detail.ticket.safety_check_snapshot && (
                  <>
                    <Typography.Title level={5}>安全检测快照</Typography.Title>
                    <JsonBlock value={detail.ticket.safety_check_snapshot} />
                  </>
                )}
              </div>
            ),
          },
          {
            key: 'sap-rma',
            label: `SAP / RMA(${detail.sap_exports?.length ?? 0})`,
            children: (
              <div className="drawer-stack">
                <Space wrap>
                  <Tag color="blue">批次：{detail.sap_export_summary?.batch_status ?? '尚未提交'}</Tag>
                  <Tag>明细：{detail.sap_export_summary?.line_count ?? 0}</Tag>
                  <Tag color="cyan">已受理：{detail.sap_export_summary?.accepted_count ?? 0}</Tag>
                  <Tag color="green">已回填 RMA：{detail.sap_export_summary?.rma_received_count ?? 0}</Tag>
                  {(detail.sap_export_summary?.failed_count ?? 0) > 0 ? (
                    <Tag color="red">失败：{detail.sap_export_summary?.failed_count}</Tag>
                  ) : null}
                </Space>
                <Space wrap>
                  <Tag color={detail.rma_issue_summary?.rma_received ? 'green' : 'default'}>正式RMA</Tag>
                  <Tag color={detail.rma_issue_summary?.pdf_validated ? 'green' : 'default'}>PDF校验</Tag>
                  <Tag color={detail.rma_issue_summary?.smtp_sent ? 'green' : 'default'}>SMTP发送</Tag>
                  <Tag color={detail.rma_issue_summary?.message_id_saved ? 'green' : 'default'}>Message-ID</Tag>
                  <Tag color={detail.rma_issue_summary?.pdf_archived ? 'green' : 'default'}>PDF归档</Tag>
                  <Tag color={detail.rma_issue_summary?.outbound_archived ? 'green' : 'default'}>邮件归档</Tag>
                  <Tag color={detail.rma_issue_summary?.closed ? 'green' : 'orange'}>签发闭环</Tag>
                </Space>
                <Table
                  size="small"
                  rowKey="id"
                  dataSource={detail.sap_exports ?? []}
                  pagination={false}
                  scroll={{ x: 1450 }}
                  columns={[
                    { title: 'SN', dataIndex: 'sn', width: 150, fixed: 'left', render: (v?: string) => <CopyableField value={v || '-'} /> },
                    { title: '状态', dataIndex: 'status', width: 120, render: (v: string) => <StatusTag value={v} kind="sap" /> },
                    { title: 'CallID', dataIndex: 'remote_call_id', width: 150, render: (v?: string) => <CopyableField value={v || '-'} /> },
                    { title: 'RMA', dataIndex: 'rma_no', width: 130, render: (v?: string) => <CopyableField value={v || '-'} /> },
                    { title: '客户代码', dataIndex: 'customer_code', width: 120, render: (v?: string) => v || '-' },
                    { title: 'SAP 物料代码', dataIndex: 'material_code', width: 140, render: (v?: string) => v || '-' },
                    { title: '收费状态', dataIndex: 'charge_status', width: 130, render: (v?: string) => v || '-' },
                    { title: '客户方邮寄地址', dataIndex: 'mailing_address', width: 220, ellipsis: true, render: (v?: string) => v || '-' },
                    { title: '维修费', dataIndex: 'repair_fee', width: 100, render: (v?: number | string) => v ?? '-' },
                    { title: '币种', dataIndex: 'currency', width: 80, render: (v?: string) => v || '-' },
                    { title: '税率', dataIndex: 'tax_rate', width: 80, render: (v?: number | string) => v == null ? '-' : `${v}%` },
                    { title: '快递费规则', dataIndex: 'shipping_fee', width: 170, render: (v?: string) => v || '-' },
                    { title: '尝试', dataIndex: 'attempt_count', width: 70 },
                    { title: '提交时间', dataIndex: 'submitted_at', width: 160, render: formatTime },
                    { title: '受理时间', dataIndex: 'accepted_at', width: 160, render: formatTime },
                    { title: 'RMA 回填时间', dataIndex: 'rma_received_at', width: 160, render: formatTime },
                    {
                      title: '失败原因',
                      width: 260,
                      render: (_: unknown, row: NonNullable<TicketDetail['sap_exports']>[number]) => (
                        row.last_error_code
                          ? <Typography.Text type="danger">{row.last_error_code}: {row.last_error_message || '-'}</Typography.Text>
                          : '-'
                      ),
                    },
                    {
                      title: '恢复操作',
                      width: 150,
                      fixed: 'right',
                      render: (_: unknown, row: NonNullable<TicketDetail['sap_exports']>[number]) => (
                        row.status === 'submit_uncertain' && canTransitionTicket
                          ? <Button size="small" danger onClick={() => onReconcileSap(row.id)}>人工对账</Button>
                          : '-'
                      ),
                    },
                  ]}
                />
                <Typography.Title level={5}>RMA 记录</Typography.Title>
                <Table
                  size="small"
                  rowKey="id"
                  dataSource={detail.rma_records ?? []}
                  pagination={false}
                  columns={[
                    { title: 'RMA 编号', dataIndex: 'rma_no', width: 150, render: (v: string) => <CopyableField value={v} /> },
                    { title: '状态', dataIndex: 'status', width: 130, render: (v: string) => <StatusTag value={v} kind="rma" /> },
                    { title: '接收时间', dataIndex: 'received_at', width: 170, render: formatTime },
                    { title: '发送时间', dataIndex: 'sent_at', width: 170, render: formatTime },
                    { title: 'PDF校验', dataIndex: 'pdf_validation_status', width: 120, render: (v?: string) => <StatusTag value={v || 'pending'} kind="validation" /> },
                    { title: 'PDF归档', dataIndex: 'pdf_archive_status', width: 120, render: (v?: string) => <StatusTag value={v || 'pending'} /> },
                    { title: '归档时间', dataIndex: 'pdf_archived_at', width: 170, render: formatTime },
                    { title: '签发完成', dataIndex: 'issued_at', width: 170, render: formatTime },
                    { title: 'PDF 对象', dataIndex: 'pdf_oss_object_id', width: 120, render: (v?: number) => v || '-' },
                    { title: '回复记录', dataIndex: 'reply_record_id', width: 120, render: (v?: number) => v || '-' },
                  ]}
                />
                {canTransitionTicket ? (
                  <Space wrap>
                    <Button danger loading={retrySapLoading} onClick={onRetrySap}>重试 SAP 提交</Button>
                    <Button icon={<SyncOutlined />} loading={pollSapLoading} onClick={onPollSap}>重新轮询</Button>
                    <Button loading={confirmLateSapLoading} onClick={onConfirmLateSap}>确认迟到结果</Button>
                    <Button type="primary" loading={retryRmaLoading} onClick={onRetryRma}>继续RMA签发/恢复归档</Button>
                    <Button loading={approveRmaManualPolicyLoading} onClick={onApproveRmaManualPolicy}>确认特殊政策并生成待审草稿</Button>
                  </Space>
                ) : null}
              </div>
            ),
          },
          {
            key: 'history',
            label: '操作历史',
            children: (
              <div className="drawer-stack">
                <Typography.Title level={5}>状态日志</Typography.Title>
                <Table<StatusLog> size="small" rowKey="id" dataSource={detail.status_logs} pagination={false}
                  columns={[
                    { title: '来源', dataIndex: 'from_status_code', width: 130, render: (v?: string) => <StatusTag value={v} kind="ticket" /> },
                    { title: '目标', dataIndex: 'to_status_code', width: 130, render: (v: string) => <StatusTag value={v} kind="ticket" /> },
                    { title: '事件', dataIndex: 'trigger_event', width: 180 },
                    { title: '原因', dataIndex: 'reason', ellipsis: true, render: (v?: string) => v || '-' },
                    { title: '时间', dataIndex: 'created_at', width: 160, render: formatTime },
                  ]}
                />
                <Typography.Title level={5}>外部操作</Typography.Title>
                <Table
                  size="small"
                  rowKey="id"
                  dataSource={detail.external_operations ?? []}
                  pagination={false}
                  scroll={{ x: 1100 }}
                  columns={[
                    { title: '操作', dataIndex: 'operation_type', width: 180 },
                    { title: '状态', dataIndex: 'status', width: 130, render: (v: string) => <StatusTag value={v} /> },
                    { title: '尝试', dataIndex: 'attempt_count', width: 70 },
                    { title: '远端标识', dataIndex: 'remote_reference', width: 190, render: (v?: string) => <CopyableField value={v || '-'} /> },
                    { title: '恢复节点', dataIndex: 'recovery_stage', width: 170, render: (v?: string) => v || '-' },
                    { title: '错误', width: 260, render: (_: unknown, row: NonNullable<TicketDetail['external_operations']>[number]) => row.error_code ? `${row.error_code}: ${row.error_message || '-'}` : '-' },
                    { title: '完成时间', dataIndex: 'completed_at', width: 170, render: formatTime },
                  ]}
                />
                <Typography.Title level={5}>人工任务</Typography.Title>
                <Table<ManualTask> size="small" rowKey="id" dataSource={detail.manual_tasks} pagination={false}
                  columns={[
                    { title: '类型', dataIndex: 'task_type', width: 150 },
                    { title: '状态', dataIndex: 'status', width: 110, render: (v: string) => <StatusTag value={v} kind="task" /> },
                    { title: '优先级', dataIndex: 'priority', width: 100, render: (v: string) => <StatusTag value={v} kind="priority" /> },
                    { title: '描述', dataIndex: 'description', ellipsis: true, render: (v?: string) => v || '-' },
                    { title: '触发原因', dataIndex: 'trigger_reason', ellipsis: true, render: (v?: string) => v || '-' },
                    { title: '恢复节点', dataIndex: 'recovery_stage', width: 170, render: (v?: string) => v || '-' },
                    { title: '处理方式', dataIndex: 'recovery_action', width: 360, render: (v?: string) => v || '-' },
                    { title: '创建时间', dataIndex: 'created_at', width: 160, render: formatTime },
                  ]}
                />
                <Typography.Title level={5}>回复记录</Typography.Title>
                <Table<ReplyRecord> size="small" rowKey="id" dataSource={detail.reply_records} pagination={false}
                  columns={[
                    { title: '类型', dataIndex: 'reply_type', width: 110 },
                    { title: '轮次', dataIndex: 'followup_round', width: 70 },
                    { title: '收件人', dataIndex: 'to_addresses', ellipsis: true },
                    { title: '主题', dataIndex: 'subject', ellipsis: true, render: (v?: string) => v || '-' },
                    { title: '审核', dataIndex: 'review_status', width: 110, render: (v: string) => <StatusTag value={v} kind="review" /> },
                    { title: '发送', dataIndex: 'send_status', width: 110, render: (v: string) => <StatusTag value={v} /> },
                    { title: '创建时间', dataIndex: 'created_at', width: 160, render: formatTime },
                  ]}
                  expandable={{
                    expandedRowRender: (record: ReplyRecord) => (
                      <div className="evidence-grid">
                        <div><Typography.Text strong>草稿内容</Typography.Text><pre className="json-block">{record.draft_body || '-'}</pre></div>
                        <div><Typography.Text strong>最终内容</Typography.Text><pre className="json-block">{record.final_body || '-'}</pre></div>
                        <div><Typography.Text strong>回复模板版本</Typography.Text><Typography.Text>{record.reply_template_version || '-'}</Typography.Text></div>
                        <div><Typography.Text strong>RMA 模板版本</Typography.Text><Typography.Text>{record.rma_template_version || '-'}</Typography.Text></div>
                        {record.error_message && (
                          <div><Typography.Text strong type="danger">错误信息</Typography.Text><pre className="json-block">{record.error_message}</pre></div>
                        )}
                      </div>
                    ),
                  }}
                />
              </div>
            ),
          },
        ]}
      />
      <div className="ticket-action-bar">
        <Button icon={<EditOutlined />} onClick={onEditFields}>字段修正</Button>
        <Button icon={<FileAddOutlined />} onClick={onAddItem}>新增明细</Button>
        <Button icon={<CheckCircleOutlined />} loading={validateLoading} onClick={onValidateSn}>SN 校验</Button>
        <Button loading={resolvePolicyLoading} onClick={onResolvePolicy}>自动解析政策</Button>
        <Button onClick={onOverridePolicy}>人工确认政策</Button>
        <Button loading={resolveReturnRoutesLoading} onClick={onResolveReturnRoutes}>匹配寄回地址</Button>
        <Button icon={<MailOutlined />} loading={draftLoading} onClick={onDraftReply}>生成追问</Button>
        <Button icon={<CheckCircleOutlined />} loading={validateExportLoading} onClick={onValidateExport}>完整安全校验</Button>
        <Button icon={<CheckCircleOutlined />} loading={confirmDeviceLoading} onClick={onConfirmDeviceReceived}>记录收货事实（不流转）</Button>
        {canTransitionTicket ? <Button type="primary" onClick={onTransition}>状态流转</Button> : null}
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
