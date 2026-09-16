import { DeleteOutlined, DownloadOutlined, ReloadOutlined, SearchOutlined, SwapOutlined, SyncOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, DatePicker, Descriptions, Drawer, Form, Input, Modal, Select, Space, Table, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api, apiErrorMessage } from '../api/client';
import ErrorResult from '../components/ErrorResult';
import { DeletionImpactPreview } from '../components/FriendlyPreview';
import JsonBlock from '../components/JsonBlock';
import ContentPreviewButton from '../components/ContentPreviewButton';
import CopyableField from '../components/CopyableField';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import { useAuthStore } from '../stores/authStore';
import type { Attachment, EmailItem, ParseResult } from '../types/api';
import { ARCHIVE_DOWNLOAD_WARNING, attachmentTypeLabel, isEngineeringReference } from '../utils/attachments';
import { filtersWithDateRange } from '../utils/filters';
import { compactText, formatFileSizeKb, formatTime, numberText } from '../utils/format';
import { rememberJob, waitForJob } from '../utils/jobs';
import { hasAnyRole } from '../utils/roles';

type EmailFilters = {
  subject?: string;
  from_address?: string;
  message_id?: string;
  parse_status?: string;
  intent_type?: string;
  handling_level?: string;
  date_range?: unknown;
};

export default function EmailsPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const user = useAuthStore((state) => state.user);
  const canFetchImap = hasAnyRole(user?.roles, ['admin', 'operator']);
  const canPreflightImap = hasAnyRole(user?.roles, ['admin']);
  const canDelete = hasAnyRole(user?.roles, ['admin']);
  const [filters, setFilters] = useState<Record<string, unknown>>({});
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<number | null>(() => {
    const value = Number(searchParams.get('email_id'));
    return Number.isInteger(value) && value > 0 ? value : null;
  });
  const [filterForm] = Form.useForm<EmailFilters>();
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
  const linkedTicketsQuery = useQuery({
    queryKey: ['email-linked-tickets', selectedId],
    queryFn: () => api.linkedTickets(selectedId as number),
    enabled: Boolean(selectedId),
  });
  useEffect(() => {
    const value = Number(searchParams.get('email_id'));
    setSelectedId(Number.isInteger(value) && value > 0 ? value : null);
  }, [searchParams]);
  const openEmail = (id: number) => navigate(`/emails?email_id=${id}`);
  const closeEmail = () => navigate('/emails');
  const fetchStatusQuery = useQuery({
    queryKey: ['imap-fetch-status'],
    queryFn: api.fetchEmailStatus,
    enabled: canFetchImap,
    refetchInterval: 5000,
  });
  const reparseMutation = useMutation({
    mutationFn: async (id: number) => {
      const job = await api.reparseEmailJob(id);
      rememberJob(job);
      return job;
    },
    onSuccess: () => {
      message.success('重新解析任务已排队');
    },
    onError: handleMutationError,
  });
  const fetchImapMutation = useMutation({
    mutationFn: async () => {
      const result = await api.fetchEmailJob();
      let completedJob = null;
      if (!result.reused) {
        rememberJob(result.job);
        completedJob = await waitForJob(result.job);
      }
      return { ...result, completedJob };
    },
    onSuccess: (result) => {
      const partial = result.completedJob?.result_json?.status === 'partial_success'
        || Number(result.completedJob?.result_json?.failed_count ?? 0) > 0;
      if (result.reused) message.info('已有邮件捞取任务正在执行');
      else if (partial) message.warning('邮件捞取完成，部分邮件失败并已进入重试队列');
      else message.success('邮件捞取完成');
      void queryClient.invalidateQueries({ queryKey: ['emails'] });
      void queryClient.invalidateQueries({ queryKey: ['tickets'] });
      void queryClient.invalidateQueries({ queryKey: ['imap-fetch-status'] });
    },
    onError: handleMutationError,
  });
  const imapPreflightMutation = useMutation({
    mutationFn: api.preflightImap,
    onSuccess: (result) => {
      Modal.success({
        title: 'IMAP 只读预检通过',
        content: (
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="账号">{result.mailbox_account}</Descriptions.Item>
            <Descriptions.Item label="文件夹">{result.folder}</Descriptions.Item>
            <Descriptions.Item label="UIDVALIDITY">{result.uid_validity}</Descriptions.Item>
            <Descriptions.Item label="读取行为">未搜索、未下载、未修改已读状态</Descriptions.Item>
            <Descriptions.Item label="OSS">已配置</Descriptions.Item>
          </Descriptions>
        ),
      });
    },
    onError: handleMutationError,
  });
  const rawEmlDownloadMutation = useMutation({
    mutationFn: (id: number) => api.rawEmlDownloadUrl(id),
    onSuccess: (data) => {
      window.open(data.url, '_blank', 'noopener,noreferrer');
    },
    onError: handleMutationError,
  });
  const attachmentDownloadMutation = useMutation({
    mutationFn: (id: number) => api.attachmentDownloadUrl(id),
    onSuccess: (data) => {
      window.open(data.url, '_blank', 'noopener,noreferrer');
    },
    onError: handleMutationError,
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
  const goToLinkedTicket = async (emailId: number) => {
    try {
      const links = await api.linkedTickets(emailId);
      if (!links.length) return void message.info('该邮件暂未关联工单');
      if (links.length === 1) return void navigate(`/tickets?ticket_id=${links[0].ticket_id}`);
      Modal.info({
        title: '选择关联工单',
        content: <Space direction="vertical">{links.map((link) => <Button key={link.ticket_id} type="link" onClick={() => navigate(`/tickets?ticket_id=${link.ticket_id}`)}>{link.ticket_no}（{link.current_status_code}）</Button>)}</Space>,
        okText: '关闭',
      });
    } catch (error) {
      message.error(apiErrorMessage(error));
    }
  };
  const confirmDeleteEmail = async (emailId: number) => {
    try {
      const preview = await api.emailDeletePreview(emailId);
      Modal.confirm({
        title: '确认删除该邮件？',
        width: 620,
        okText: '确认删除',
        okButtonProps: { danger: true, disabled: !preview.deletable },
        content: <DeletionImpactPreview preview={preview} />,
        onOk: async () => {
          await api.deleteEmail(emailId, preview.confirmation_token ?? '');
          message.success('邮件已删除');
          closeEmail();
          await Promise.all([queryClient.invalidateQueries({ queryKey: ['emails'] }), queryClient.invalidateQueries({ queryKey: ['tickets'] })]);
        },
      });
    } catch (error) {
      message.error(apiErrorMessage(error));
    }
  };

  const columns: ColumnsType<EmailItem> = [
    { title: '主题', dataIndex: 'subject', ellipsis: true, render: (value?: string) => value || '-' },
    { title: '发件人', dataIndex: 'from_address', ellipsis: true, width: 220 },
    { title: '层级', dataIndex: 'handling_level', width: 125, render: (value?: string) => <StatusTag value={value} /> },
    { title: '意图', dataIndex: 'intent_type', width: 170, render: (value?: string) => <StatusTag value={value} /> },
    { title: '解析', dataIndex: 'parse_status', width: 110, render: (value: string) => <StatusTag value={value} kind="parse" /> },
    { title: '收信时间', dataIndex: 'received_at', width: 160, render: formatTime },
    {
      title: '操作',
      width: 130,
      render: (_, record) => (
        <Space>
          <Button type="link" size="small" onClick={() => openEmail(record.id)}>
            详情
          </Button>
          <Button type="link" size="small" onClick={() => void goToLinkedTicket(record.id)}>工单</Button>
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
          {canDelete ? <Button type="link" danger size="small" icon={<DeleteOutlined />} onClick={() => void confirmDeleteEmail(record.id)} /> : null}
        </Space>
      ),
    },
  ];

  return (
    <div className="page-stack">
      <PageTitle
        title="邮件中心"
        extra={(
          <Space>
            <Button icon={<SwapOutlined />} onClick={() => navigate('/tickets')}>切换到工单中心</Button>
            {canPreflightImap && (
              <Button
                loading={imapPreflightMutation.isPending}
                onClick={() => imapPreflightMutation.mutate()}
              >
                只读预检
              </Button>
            )}
            {canFetchImap && (
              <Button
                icon={<SyncOutlined spin={fetchImapMutation.isPending} />}
                loading={fetchImapMutation.isPending}
                disabled={Boolean(fetchStatusQuery.data?.active_job) || fetchStatusQuery.data?.configured === false}
                onClick={() => fetchImapMutation.mutate()}
              >
                立即捞取
              </Button>
            )}
          </Space>
        )}
      />
      {canFetchImap && (
        <SectionPanel>
          <Descriptions column={4} size="small" bordered>
            <Descriptions.Item label="自动捞取">{fetchStatusQuery.data?.enabled ? '已开启' : '已关闭'}</Descriptions.Item>
            <Descriptions.Item label="收信账号">{fetchStatusQuery.data?.mailbox_account ?? '-'}</Descriptions.Item>
            <Descriptions.Item label="轮询策略">
              {fetchStatusQuery.data ? `${fetchStatusQuery.data.poll_interval_minutes} 分钟 / 每批 ${fetchStatusQuery.data.fetch_limit} 封` : '-'}
            </Descriptions.Item>
            <Descriptions.Item label="当前任务">
              <StatusTag value={fetchStatusQuery.data?.active_job?.status ?? fetchStatusQuery.data?.latest_job?.status ?? 'idle'} />
            </Descriptions.Item>
            <Descriptions.Item label="文件夹">{fetchStatusQuery.data?.folder ?? '-'}</Descriptions.Item>
            <Descriptions.Item label="读取方式">只读 / UNSEEN / BODY.PEEK[]</Descriptions.Item>
            <Descriptions.Item label="OSS 归档">{fetchStatusQuery.data?.archive_to_oss ? '强制开启' : '未配置'}</Descriptions.Item>
            <Descriptions.Item label="失败 / 待重试">
              {fetchStatusQuery.data ? `${fetchStatusQuery.data.latest_job?.failed_count ?? 0} / ${fetchStatusQuery.data.retry_count}` : '-'}
            </Descriptions.Item>
          </Descriptions>
        </SectionPanel>
      )}
      <SectionPanel>
        <Form<EmailFilters>
          form={filterForm}
          layout="inline"
          className="filter-bar"
          onFinish={(values) => {
            setPage(1);
            setSelectedId(null);
            setFilters(filtersWithDateRange(values, 'date_range', 'received_start', 'received_end'));
          }}
        >
          <Form.Item name="subject">
            <Input allowClear prefix={<SearchOutlined />} placeholder="主题" />
          </Form.Item>
          <Form.Item name="from_address">
            <Input allowClear placeholder="发件人" />
          </Form.Item>
          <Form.Item name="message_id">
            <Input allowClear placeholder="Message-ID" />
          </Form.Item>
          <Form.Item name="intent_type">
            <Select
              allowClear
              placeholder="邮件意图"
              style={{ width: 150 }}
              options={[
                { value: 'new_repair', label: '新报修' },
                { value: 'thread_new_repair', label: '回复链新报修' },
                { value: 'customer_supplement', label: '客户补充' },
                { value: 'component_replacement_repair', label: '物料/元器件替换维修' },
                { value: 'onsite_service', label: '叫修/现场服务' },
                { value: 'warranty_status_inquiry', label: '保修状态确认' },
                { value: 'repair_thread_other', label: '报修线程其他问题' },
                { value: 'device_intake_received', label: '待修设备到达/入库' },
                { value: 'repaired_device_dispatched', label: '维修完成设备发出' },
                { value: 'customer_repaired_device_received', label: '客户收到维修设备' },
                { value: 'contract_confirmation', label: '合同确认' },
                { value: 'invoice', label: '发票' },
                { value: 'third_party_equipment_quote', label: '非我司设备报价单' },
                { value: 'unknown', label: '未知' },
              ]}
            />
          </Form.Item>
          <Form.Item name="handling_level">
            <Select
              allowClear
              placeholder="处理层级"
              style={{ width: 155 }}
              options={[
                { value: 'auto_repair', label: 'FIRST 自动报修' },
                { value: 'manual_rma_business', label: 'SECOND 人工业务' },
                { value: 'lifecycle_only', label: 'THIRD 生命周期' },
                { value: 'unknown', label: 'UNKNOWN 未知' },
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
        <Table<EmailItem>
          rowKey="id"
          columns={columns}
          dataSource={emailsQuery.data?.items ?? []}
          loading={emailsQuery.isFetching}
          locale={{
            emptyText: emailsQuery.isError
              ? <ErrorResult message={apiErrorMessage(emailsQuery.error)} onRetry={() => emailsQuery.refetch()} />
              : '暂无邮件'
          }}
          pagination={{ current: page, pageSize: 20, total: emailsQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
        />
      </SectionPanel>
      <Drawer width={760} title="邮件详情" open={Boolean(selectedId)} onClose={closeEmail} extra={selectedId && canDelete ? <Button danger icon={<DeleteOutlined />} onClick={() => void confirmDeleteEmail(selectedId)}>删除</Button> : null}>
        {detailQuery.data ? (
          <div className="drawer-stack">
            <Descriptions column={1} size="small" bordered>
              <Descriptions.Item label="关联工单">{linkedTicketsQuery.data?.length ? <Space wrap>{linkedTicketsQuery.data.map((link) => <Button key={link.ticket_id} type="link" onClick={() => navigate(`/tickets?ticket_id=${link.ticket_id}`)}>{link.ticket_no}</Button>)}</Space> : '-'}</Descriptions.Item>
              <Descriptions.Item label="主题">{detailQuery.data.email.subject || '-'}</Descriptions.Item>
              <Descriptions.Item label="发件人"><CopyableField value={detailQuery.data.email.from_address} /></Descriptions.Item>
              <Descriptions.Item label="收件人">{detailQuery.data.email.to_addresses || '-'}</Descriptions.Item>
              <Descriptions.Item label="Message-ID"><CopyableField value={detailQuery.data.email.message_id || ''} displayText={detailQuery.data.email.message_id || '-'} /></Descriptions.Item>
              <Descriptions.Item label="意图">{detailQuery.data.email.intent_type || '-'}</Descriptions.Item>
              <Descriptions.Item label="处理层级">{detailQuery.data.email.handling_level || '-'}</Descriptions.Item>
              <Descriptions.Item label="分类版本">{detailQuery.data.email.classification_version || '-'}</Descriptions.Item>
              <Descriptions.Item label="原始EML">
                {detailQuery.data.email.raw_eml_oss_object_id ? (
                  <Button
                    size="small"
                    icon={<DownloadOutlined />}
                    loading={rawEmlDownloadMutation.isPending}
                    onClick={() => rawEmlDownloadMutation.mutate(detailQuery.data.email.id)}
                  >
                    Download
                  </Button>
                ) : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="正文">{compactText(detailQuery.data.email.clean_body, '-')}</Descriptions.Item>
              <Descriptions.Item label="正文预览"><ContentPreviewButton kind="email" id={detailQuery.data.email.id} /></Descriptions.Item>
            </Descriptions>
            <div>
              <Typography.Title level={5}>附件</Typography.Title>
              <Table<Attachment>
                size="small"
                rowKey="id"
                dataSource={detailQuery.data.attachments}
                pagination={false}
                columns={[
                  { title: '文件名', dataIndex: 'file_name', ellipsis: true },
                  { title: '类型', dataIndex: 'content_type', width: 150, render: (value?: string) => value || '-' },
                  { title: '附件类型', width: 170, render: (_, record) => attachmentTypeLabel(record) },
                  { title: '发送时间', dataIndex: 'sent_at', width: 150, render: formatTime },
                  { title: '大小', dataIndex: 'file_size_kb', width: 100, render: (_value, record) => formatFileSizeKb(record.file_size_kb, record.file_size) },
                  { title: '状态', dataIndex: 'parse_status', width: 150, render: (value: string, record) => <StatusTag value={isEngineeringReference(record) ? 'engineering_reference_stored' : value} kind="parse" /> },
                  { title: '安全提示', width: 140, render: (_, record) => isEngineeringReference(record) ? <Typography.Text type="warning">未经内容扫描</Typography.Text> : '-' },
                  {
                    title: '操作',
                    width: 120,
                    render: (_, record) => (
                      <Space size={0}>
                        <ContentPreviewButton kind="attachment" id={record.id} disabled={!record.oss_object_id} />
                        <Button
                          type="link"
                          size="small"
                          icon={<DownloadOutlined />}
                          disabled={!record.oss_object_id}
                          loading={attachmentDownloadMutation.isPending}
                          onClick={() => downloadAttachment(record)}
                          title="下载"
                        />
                      </Space>
                    ),
                  },
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
            </div>
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
    </div>
  );
}
