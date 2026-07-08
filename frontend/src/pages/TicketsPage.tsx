import {
  CheckCircleOutlined,
  DownloadOutlined,
  EditOutlined,
  FileAddOutlined,
  MailOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Button,
  DatePicker,
  Descriptions,
  Drawer,
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
import { useState } from 'react';
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
  ParseResult,
  ReplyRecord,
  SnValidationResult,
  StatusLog,
  Ticket,
  TicketDetail,
  TicketLine,
} from '../types/api';
import { compactFilters, filtersWithDateRange } from '../utils/filters';
import { compactText, formatTime, numberText } from '../utils/format';
import { saveBlob } from '../utils/download';
import { hasAnyRole } from '../utils/roles';
import { ticketStatusLabels } from '../utils/status';

type TicketFilters = {
  ticket_no?: string;
  customer?: string;
  contact?: string;
  sn?: string;
  assigned_user_id?: string;
  status_code?: string;
  date_range?: unknown;
};

type TransitionForm = {
  to_status_code: string;
  trigger_event: string;
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

const transitionOptions = [
  { to_status_code: 'manual_review', trigger_event: 'manual_review_required', label: '转人工复核' },
  { to_status_code: 'ready_for_export', trigger_event: 'validation_passed', label: '标记可导出' },
  { to_status_code: 'need_customer_info', trigger_event: 'missing_fields_detected', label: '等待客户补充' },
  { to_status_code: 'closed', trigger_event: 'manual_close', label: '人工关闭' },
  { to_status_code: 'error', trigger_event: 'system_error', label: '标记异常' },
];

const lockOptions = [
  { value: false, label: '未锁定' },
  { value: true, label: '人工锁定' },
];

export default function TicketsPage() {
  const [filters, setFilters] = useState<Record<string, unknown>>({});
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [transitionOpen, setTransitionOpen] = useState(false);
  const [fieldOpen, setFieldOpen] = useState(false);
  const [itemOpen, setItemOpen] = useState(false);
  const [editingItem, setEditingItem] = useState<TicketLine | null>(null);
  const [filterForm] = Form.useForm<TicketFilters>();
  const queryClient = useQueryClient();
  const canTransitionTicket = hasAnyRole(useAuthStore((state) => state.user?.roles), ['admin', 'supervisor']);
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
  const detailQuery = useQuery({
    queryKey: ['ticket-detail', selectedId],
    queryFn: () => api.ticketDetail(selectedId as number),
    enabled: Boolean(selectedId),
  });

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
    mutationFn: ({ id, action }: { id: number; action: 'apply' | 'reject' }) => api.applyParseResult(id, {
      action,
      reason: action === 'reject' ? '前端人工拒绝解析候选' : '前端人工采纳解析候选',
    }),
    onSuccess: () => {
      message.success('解析候选状态已更新');
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
  const exportMutation = useMutation({
    mutationFn: () => api.exportTickets(compactFilters(filters)),
    onSuccess: (blob) => saveBlob(blob, 'tickets-export.xlsx'),
    onError: handleMutationError,
  });

  const openItemEditor = (item?: TicketLine) => {
    setEditingItem(item ?? null);
    setItemOpen(true);
  };

  const columns: ColumnsType<Ticket> = [
    { title: '工单号', dataIndex: 'ticket_no', width: 170 },
    { title: '状态', dataIndex: 'current_status_code', width: 130, render: (value: string) => <StatusTag value={value} kind="ticket" /> },
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
          <Button icon={<DownloadOutlined />} loading={exportMutation.isPending} onClick={() => exportMutation.mutate()}>
            导出
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
          <Form.Item name="assigned_user_id">
            <Input allowClear type="number" placeholder="处理人 ID" />
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
          locale={{ emptyText: ticketsQuery.isError ? '工单加载失败' : '暂无工单' }}
          pagination={{ current: page, pageSize: 20, total: ticketsQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
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
        extra={
          detailQuery.data ? (
            <Space wrap>
              <Button icon={<FileAddOutlined />} onClick={() => openItemEditor()}>
                新增明细
              </Button>
              <Button icon={<EditOutlined />} onClick={() => setFieldOpen(true)}>
                编辑字段
              </Button>
              <Button
                icon={<CheckCircleOutlined />}
                loading={validateMutation.isPending}
                onClick={() => confirmAction('确认执行 SN 校验？', () => validateMutation.mutate(detailQuery.data.ticket.id))}
              >
                SN 校验
              </Button>
              <Button
                icon={<MailOutlined />}
                loading={draftReplyMutation.isPending}
                onClick={() => confirmAction('确认生成追问草稿？', () => draftReplyMutation.mutate())}
              >
                生成追问
              </Button>
              {canTransitionTicket ? (
                <Button type="primary" onClick={() => setTransitionOpen(true)}>
                  状态流转
                </Button>
              ) : null}
            </Space>
          ) : null
        }
      >
        {detailQuery.data ? (
          <TicketDetailView
            detail={detailQuery.data}
            onApplyParse={(id) => confirmAction('确认采纳该解析候选？', () => applyParseMutation.mutate({ id, action: 'apply' }))}
            onRejectParse={(id) => confirmAction('确认拒绝该解析候选？', () => applyParseMutation.mutate({ id, action: 'reject' }))}
            onEditItem={openItemEditor}
          />
        ) : detailQuery.isFetching ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="正在加载工单详情" />
        ) : detailQuery.isError ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="工单详情加载失败" />
        ) : null}
      </Drawer>
      <Modal title="编辑工单字段" open={fieldOpen} onCancel={() => setFieldOpen(false)} footer={null} destroyOnClose>
        {detailQuery.data ? (
          <Form<TicketFieldForm>
            layout="vertical"
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
            <Button type="primary" htmlType="submit" loading={patchFieldsMutation.isPending}>
              保存
            </Button>
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
          <Button type="primary" htmlType="submit" loading={patchItemsMutation.isPending}>
            保存
          </Button>
        </Form>
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
    </div>
  );
}

function TicketDetailView({
  detail,
  onApplyParse,
  onRejectParse,
  onEditItem,
}: {
  detail: TicketDetail;
  onApplyParse: (id: number) => void;
  onRejectParse: (id: number) => void;
  onEditItem: (item: TicketLine) => void;
}) {
  const timelineEmails = detail.email_timeline.length > 0 ? detail.email_timeline : detail.source_email ? [detail.source_email] : [];
  const fieldAudits = detail.field_evidence?.field_audits ?? [];

  return (
    <div className="drawer-stack">
      <Descriptions column={3} size="small" bordered>
        <Descriptions.Item label="工单号">{detail.ticket.ticket_no}</Descriptions.Item>
        <Descriptions.Item label="状态"><StatusTag value={detail.ticket.current_status_code} kind="ticket" /></Descriptions.Item>
        <Descriptions.Item label="版本">{detail.ticket.version}</Descriptions.Item>
        <Descriptions.Item label="客户">{detail.ticket.customer_name || '-'}</Descriptions.Item>
        <Descriptions.Item label="客户代码">{detail.ticket.customer_code || '-'}</Descriptions.Item>
        <Descriptions.Item label="联系人">{detail.ticket.contact_person || '-'}</Descriptions.Item>
        <Descriptions.Item label="电话">{detail.ticket.contact_phone || '-'}</Descriptions.Item>
        <Descriptions.Item label="邮箱">{detail.ticket.contact_email || '-'}</Descriptions.Item>
        <Descriptions.Item label="报修日期">{detail.ticket.request_date || '-'}</Descriptions.Item>
        <Descriptions.Item label="寄送地址" span={3}>{detail.ticket.mailing_address || '-'}</Descriptions.Item>
        <Descriptions.Item label="问题描述" span={3}>{compactText(detail.ticket.problem_description, '-')}</Descriptions.Item>
      </Descriptions>
      <Tabs
        items={[
          {
            key: 'context',
            label: '基础上下文',
            children: (
              <div className="two-column-grid">
                <Descriptions column={1} size="small" bordered>
                  <Descriptions.Item label="源邮件 ID">{detail.ticket.source_email_id || '-'}</Descriptions.Item>
                  <Descriptions.Item label="邮件主题">{detail.source_email?.subject || '-'}</Descriptions.Item>
                  <Descriptions.Item label="发件人">{detail.source_email?.from_address || '-'}</Descriptions.Item>
                  <Descriptions.Item label="收件时间">{formatTime(detail.source_email?.received_at)}</Descriptions.Item>
                </Descriptions>
                <Descriptions column={1} size="small" bordered>
                  <Descriptions.Item label="Thread ID">{detail.thread?.id || detail.ticket.thread_id || '-'}</Descriptions.Item>
                  <Descriptions.Item label="Thread Key">{detail.thread?.thread_key || '-'}</Descriptions.Item>
                  <Descriptions.Item label="邮件数量">{detail.thread?.email_count ?? '-'}</Descriptions.Item>
                  <Descriptions.Item label="合并置信度">{numberText(detail.thread?.merge_confidence)}</Descriptions.Item>
                </Descriptions>
              </div>
            ),
          },
          {
            key: 'items',
            label: `明细(${detail.items.length})`,
            children: (
              <Table<TicketLine>
                size="small"
                rowKey="id"
                dataSource={detail.items}
                pagination={false}
                columns={[
                  { title: '行号', dataIndex: 'line_no', width: 70 },
                  { title: 'SN', dataIndex: 'sn', width: 150, render: (value?: string) => value || '-' },
                  { title: '物料编码', dataIndex: 'material_code', width: 130, render: (value?: string) => value || '-' },
                  { title: '物料名称', dataIndex: 'material_name', width: 160, render: (value?: string) => value || '-' },
                  { title: '数量', dataIndex: 'quantity', width: 70 },
                  { title: '校验', dataIndex: 'validation_status', width: 110, render: (value: string) => <StatusTag value={value} /> },
                  { title: '校验说明', dataIndex: 'validation_message', ellipsis: true, render: (value?: string) => value || '-' },
                  { title: '故障描述', dataIndex: 'failure_description', ellipsis: true, render: (value?: string) => compactText(value) },
                  { title: '操作', width: 90, render: (_, record) => <Button type="link" size="small" onClick={() => onEditItem(record)}>编辑</Button> },
                ]}
              />
            ),
          },
          {
            key: 'parse',
            label: `解析候选(${detail.parse_results.length})`,
            children: (
              <Table<ParseResult>
                size="small"
                rowKey="id"
                dataSource={detail.parse_results}
                pagination={false}
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
                        <Typography.Text strong>字段证据</Typography.Text>
                        <JsonBlock value={record.evidence} />
                      </div>
                    </div>
                  ),
                }}
                columns={[
                  { title: 'ID', dataIndex: 'id', width: 70 },
                  { title: '解析器', dataIndex: 'parser_type', width: 100 },
                  { title: '意图', dataIndex: 'intent_type', width: 130, render: (value?: string) => value || '-' },
                  { title: '置信度', dataIndex: 'confidence_score', width: 90, render: numberText },
                  { title: '应用状态', dataIndex: 'apply_status', width: 120, render: (value: string) => <StatusTag value={value} /> },
                  { title: '缺失字段', dataIndex: 'missing_fields', render: (value) => <JsonBlock value={value} /> },
                  { title: '创建时间', dataIndex: 'created_at', width: 160, render: formatTime },
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
              />
            ),
          },
          {
            key: 'sn',
            label: `SN 校验(${detail.sn_validation_results.length})`,
            children: (
              <Table<SnValidationResult>
                size="small"
                rowKey="id"
                dataSource={detail.sn_validation_results}
                pagination={false}
                columns={[
                  { title: 'SN', dataIndex: 'sn', width: 150, render: (value?: string) => value || '-' },
                  { title: '结果', dataIndex: 'result_status', width: 110, render: (value: string) => <StatusTag value={value} /> },
                  { title: '存在', dataIndex: 'check_exists', width: 80, render: boolText },
                  { title: '有效', dataIndex: 'check_valid', width: 80, render: boolText },
                  { title: '客户匹配', dataIndex: 'check_customer_match', width: 100, render: boolText },
                  { title: '物料匹配', dataIndex: 'check_material_match', width: 100, render: boolText },
                  { title: '需发北京', dataIndex: 'need_ship_to_beijing', width: 100, render: boolText },
                  { title: '说明', dataIndex: 'result_message', ellipsis: true, render: (value?: string) => value || '-' },
                  { title: '校验时间', dataIndex: 'checked_at', width: 160, render: formatTime },
                ]}
              />
            ),
          },
          {
            key: 'timeline',
            label: `邮件时间线(${timelineEmails.length})`,
            children: timelineEmails.length ? (
              <Timeline
                items={timelineEmails.map((email) => ({
                  children: <EmailTimelineItem email={email} />,
                }))}
              />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ),
          },
          {
            key: 'attachments',
            label: `附件(${detail.attachments.length})`,
            children: (
              <Table<Attachment>
                size="small"
                rowKey="id"
                dataSource={detail.attachments}
                pagination={false}
                columns={[
                  { title: '文件名', dataIndex: 'file_name', ellipsis: true },
                  { title: '邮件 ID', dataIndex: 'email_id', width: 90 },
                  { title: '类型', dataIndex: 'content_type', width: 160, render: (value?: string) => value || '-' },
                  { title: '大小', dataIndex: 'file_size', width: 100, render: formatBytes },
                  { title: '解析状态', dataIndex: 'parse_status', width: 110, render: (value: string) => <StatusTag value={value} kind="parse" /> },
                  { title: '解析错误', dataIndex: 'parse_error', ellipsis: true, render: (value?: string) => value || '-' },
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
            key: 'evidence',
            label: `字段证据(${fieldAudits.length})`,
            children: (
              <div className="drawer-stack">
                <div className="two-column-grid">
                  <div>
                    <Typography.Title level={5}>缺失字段</Typography.Title>
                    <JsonBlock value={detail.ticket.missing_fields} />
                  </div>
                  <div>
                    <Typography.Title level={5}>冲突字段</Typography.Title>
                    <JsonBlock value={detail.ticket.conflict_fields} />
                  </div>
                </div>
                <Table<FieldAuditLog>
                  size="small"
                  rowKey="id"
                  dataSource={fieldAudits}
                  pagination={false}
                  columns={[
                    { title: '字段', dataIndex: 'field_name', width: 150 },
                    { title: '明细 ID', dataIndex: 'ticket_item_id', width: 90, render: (value?: number) => value || '-' },
                    { title: '旧值', dataIndex: 'old_value', ellipsis: true, render: (value?: string) => value || '-' },
                    { title: '新值', dataIndex: 'new_value', ellipsis: true, render: (value?: string) => value || '-' },
                    { title: '来源', dataIndex: 'source_type', width: 100 },
                    { title: '原因', dataIndex: 'reason', ellipsis: true, render: (value?: string) => value || '-' },
                    { title: '时间', dataIndex: 'created_at', width: 160, render: formatTime },
                  ]}
                />
              </div>
            ),
          },
          {
            key: 'replies',
            label: `回复记录(${detail.reply_records.length})`,
            children: (
              <Table<ReplyRecord>
                size="small"
                rowKey="id"
                dataSource={detail.reply_records}
                pagination={false}
                columns={[
                  { title: '类型', dataIndex: 'reply_type', width: 110 },
                  { title: '轮次', dataIndex: 'followup_round', width: 70 },
                  { title: '收件人', dataIndex: 'to_addresses', ellipsis: true },
                  { title: '主题', dataIndex: 'subject', ellipsis: true, render: (value?: string) => value || '-' },
                  { title: '审核', dataIndex: 'review_status', width: 110, render: (value: string) => <StatusTag value={value} kind="review" /> },
                  { title: '发送', dataIndex: 'send_status', width: 110, render: (value: string) => <StatusTag value={value} /> },
                  { title: '创建时间', dataIndex: 'created_at', width: 160, render: formatTime },
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
            key: 'logs',
            label: '日志与任务',
            children: (
              <div className="drawer-stack">
                <Typography.Title level={5}>状态日志</Typography.Title>
                <Table<StatusLog>
                  size="small"
                  rowKey="id"
                  dataSource={detail.status_logs}
                  pagination={false}
                  columns={[
                    { title: '来源', dataIndex: 'from_status_code', width: 130, render: (value?: string) => <StatusTag value={value} kind="ticket" /> },
                    { title: '目标', dataIndex: 'to_status_code', width: 130, render: (value: string) => <StatusTag value={value} kind="ticket" /> },
                    { title: '事件', dataIndex: 'trigger_event', width: 180 },
                    { title: '原因', dataIndex: 'reason', ellipsis: true, render: (value?: string) => value || '-' },
                    { title: '时间', dataIndex: 'created_at', width: 160, render: formatTime },
                  ]}
                />
                <Typography.Title level={5}>人工任务</Typography.Title>
                <Table<ManualTask>
                  size="small"
                  rowKey="id"
                  dataSource={detail.manual_tasks}
                  pagination={false}
                  columns={[
                    { title: '类型', dataIndex: 'task_type', width: 150 },
                    { title: '状态', dataIndex: 'status', width: 110, render: (value: string) => <StatusTag value={value} kind="task" /> },
                    { title: '优先级', dataIndex: 'priority', width: 100, render: (value: string) => <StatusTag value={value} kind="priority" /> },
                    { title: '描述', dataIndex: 'description', ellipsis: true, render: (value?: string) => value || '-' },
                    { title: '触发原因', dataIndex: 'trigger_reason', ellipsis: true, render: (value?: string) => value || '-' },
                    { title: '创建时间', dataIndex: 'created_at', width: 160, render: formatTime },
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

function boolText(value?: boolean | null) {
  if (value === true) return '是';
  if (value === false) return '否';
  return '-';
}

function formatBytes(value?: number | null) {
  if (!value) return '-';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
