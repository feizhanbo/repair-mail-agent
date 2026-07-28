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
  DatePicker,
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
  const [filterForm] = Form.useForm<TicketFilters>();
  const queryClient = useQueryClient();
  const canTransitionTicket = hasAnyRole(useAuthStore((state) => state.user?.roles), ['admin', 'supervisor', 'operator']);
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
      .filter((t) => t.enabled)
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
      message.success(result.status === 'sent' ? '收货确认已发送并完成关单' : '公司收货事实已记录');
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
  const retryRmaMutation = useMutation({
    mutationFn: (id: number) => api.retryRmaSend(id),
    onSuccess: () => {
      message.success('RMA 模板回复重发已进入后台队列');
      invalidateDetail();
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
            onRetryRma={() => confirmAction('确认按模板在原邮件链重新发送 RMA？', () => retryRmaMutation.mutate(detailQuery.data.ticket.id))}
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
  onRetryRma,
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
  onRetryRma: () => void;
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
                <Table<TicketLine>
                  size="small" rowKey="id" dataSource={detail.items} pagination={false}
                  columns={[
                    { title: '行', dataIndex: 'line_no', width: 48 },
                    { title: 'SN', dataIndex: 'sn', width: 140, render: (v?: string) => <CopyableField value={v || '-'} /> },
                    { title: '物料编码', dataIndex: 'material_code', width: 130, render: (v?: string) => v || '-' },
                    { title: '物料名称', dataIndex: 'material_name', width: 160, render: (v?: string) => v || '-' },
                    { title: '数量', dataIndex: 'quantity', width: 60 },
                    { title: '校验', dataIndex: 'validation_status', width: 100, render: (v: string) => <StatusTag value={v} /> },
                    { title: '校验说明', dataIndex: 'validation_message', ellipsis: true, render: (v?: string) => v || '-' },
                    { title: '故障描述', dataIndex: 'failure_description', ellipsis: true, render: (v?: string) => compactText(v) },
                    { title: '操作', width: 80, render: (_: unknown, r: TicketLine) => <Button type="link" size="small" onClick={() => onEditItem(r)}>编辑</Button> },
                  ]}
                />
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
                <Table
                  size="small"
                  rowKey="id"
                  dataSource={detail.sap_exports ?? []}
                  pagination={false}
                  scroll={{ x: 1450 }}
                  columns={[
                    { title: 'SN', dataIndex: 'sn', width: 150, fixed: 'left', render: (v?: string) => <CopyableField value={v || '-'} /> },
                    { title: '状态', dataIndex: 'status', width: 120, render: (v: string) => <StatusTag value={v} /> },
                    { title: 'callID', dataIndex: 'remote_call_id', width: 150, render: (v?: string) => <CopyableField value={v || '-'} /> },
                    { title: 'RMA', dataIndex: 'rma_no', width: 130, render: (v?: string) => <CopyableField value={v || '-'} /> },
                    { title: '客户代码', dataIndex: 'customer_code', width: 120, render: (v?: string) => v || '-' },
                    { title: '物料代码', dataIndex: 'material_code', width: 140, render: (v?: string) => v || '-' },
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
                    { title: '状态', dataIndex: 'status', width: 130, render: (v: string) => <StatusTag value={v} /> },
                    { title: '接收时间', dataIndex: 'received_at', width: 170, render: formatTime },
                    { title: '发送时间', dataIndex: 'sent_at', width: 170, render: formatTime },
                    { title: 'PDF 对象', dataIndex: 'pdf_oss_object_id', width: 120, render: (v?: number) => v || '-' },
                    { title: '回复记录', dataIndex: 'reply_record_id', width: 120, render: (v?: number) => v || '-' },
                  ]}
                />
                {canTransitionTicket ? (
                  <Space wrap>
                    <Button danger loading={retrySapLoading} onClick={onRetrySap}>重试 SAP 提交</Button>
                    <Button icon={<SyncOutlined />} loading={pollSapLoading} onClick={onPollSap}>重新轮询</Button>
                    <Button loading={confirmLateSapLoading} onClick={onConfirmLateSap}>确认迟到结果</Button>
                    <Button type="primary" loading={retryRmaLoading} onClick={onRetryRma}>重发 RMA</Button>
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
                <Typography.Title level={5}>人工任务</Typography.Title>
                <Table<ManualTask> size="small" rowKey="id" dataSource={detail.manual_tasks} pagination={false}
                  columns={[
                    { title: '类型', dataIndex: 'task_type', width: 150 },
                    { title: '状态', dataIndex: 'status', width: 110, render: (v: string) => <StatusTag value={v} kind="task" /> },
                    { title: '优先级', dataIndex: 'priority', width: 100, render: (v: string) => <StatusTag value={v} kind="priority" /> },
                    { title: '描述', dataIndex: 'description', ellipsis: true, render: (v?: string) => v || '-' },
                    { title: '触发原因', dataIndex: 'trigger_reason', ellipsis: true, render: (v?: string) => v || '-' },
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
        <Button icon={<MailOutlined />} loading={draftLoading} onClick={onDraftReply}>生成追问</Button>
        <Button icon={<CheckCircleOutlined />} loading={validateExportLoading} onClick={onValidateExport}>完整安全校验</Button>
        <Button icon={<CheckCircleOutlined />} loading={confirmDeviceLoading} disabled={detail.ticket.current_status_code === 'closed'} onClick={onConfirmDeviceReceived}>确认收货</Button>
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
