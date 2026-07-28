import { DownloadOutlined, PlusOutlined, ReloadOutlined, SearchOutlined, SyncOutlined, UploadOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, DatePicker, Descriptions, Drawer, Form, Input, Modal, Select, Space, Table, Typography, Upload, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api, apiErrorMessage } from '../api/client';
import ErrorResult from '../components/ErrorResult';
import JsonBlock from '../components/JsonBlock';
import ContentPreviewButton from '../components/ContentPreviewButton';
import CopyableField from '../components/CopyableField';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import { useAuthStore } from '../stores/authStore';
import type { Attachment, EmailIngestAttachment, EmailIngestRequest, EmailIngestResult, EmailItem, ParseResult } from '../types/api';
import { ARCHIVE_DOWNLOAD_WARNING, attachmentTypeLabel, isEngineeringReference } from '../utils/attachments';
import { filtersWithDateRange } from '../utils/filters';
import { compactText, formatFileSizeKb, formatTime, numberText } from '../utils/format';
import { saveBlob } from '../utils/download';
import { rememberJob, waitForJob } from '../utils/jobs';
import { hasAnyRole } from '../utils/roles';

type EmailFilters = {
  subject?: string;
  from_address?: string;
  message_id?: string;
  parse_status?: string;
  intent_type?: string;
  date_range?: unknown;
};

const emailAsyncEnabled = import.meta.env.VITE_EMAIL_ASYNC_ENABLED === 'true';

export default function EmailsPage() {
  const user = useAuthStore((state) => state.user);
  const canFetchImap = hasAnyRole(user?.roles, ['admin', 'operator']);
  const canPreflightImap = hasAnyRole(user?.roles, ['admin']);
  const [filters, setFilters] = useState<Record<string, unknown>>({});
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [ingestOpen, setIngestOpen] = useState(false);
  const [manualAttachments, setManualAttachments] = useState<EmailIngestAttachment[]>([]);
  const [filterForm] = Form.useForm<EmailFilters>();
  const queryClient = useQueryClient();
  const handleMutationError = (error: unknown) => message.error(apiErrorMessage(error));
  const ingestSuccessMessage = (data: EmailIngestResult) => {
    if (data.skipped) return '邮件已被预检查跳过';
    if (data.duplicate) return '邮件已存在，未重复入库';
    if (data.parse && typeof data.parse === 'object' && 'manual_ticket' in data.parse) return '邮件已入库并进入人工复核';
    return '邮件已入库';
  };
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
    mutationFn: async (values: EmailIngestRequest) => {
      const body = { ...values, attachments: manualAttachments };
      if (!emailAsyncEnabled) return api.ingestEmail(body);
      const result = await api.ingestEmailJob(body);
      if (result.job) await waitForJob(result.job);
      return result.ingest;
    },
    onSuccess: (data) => {
      message.success(ingestSuccessMessage(data));
      setIngestOpen(false);
      setManualAttachments([]);
      void queryClient.invalidateQueries({ queryKey: ['emails'] });
      void queryClient.invalidateQueries({ queryKey: ['tickets'] });
    },
    onError: handleMutationError,
  });
  const fetchStatusQuery = useQuery({
    queryKey: ['imap-fetch-status'],
    queryFn: api.fetchEmailStatus,
    enabled: canFetchImap,
    refetchInterval: 5000,
  });
  const ingestEmlMutation = useMutation({
    mutationFn: (file: File) => api.ingestEmlFileJob(file),
    onSuccess: ({ ingest, job }) => {
      if (job) {
        rememberJob(job);
        message.success(`邮件已归档，解析任务 #${job.id} 已排队`);
      } else {
        message.success(ingestSuccessMessage(ingest));
      }
      void queryClient.invalidateQueries({ queryKey: ['emails'] });
      void queryClient.invalidateQueries({ queryKey: ['tickets'] });
    },
    onError: handleMutationError,
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
  const exportMutation = useMutation({
    mutationFn: () => api.exportEmails(filters),
    onSuccess: (blob) => saveBlob(blob, 'emails-export.xlsx'),
    onError: handleMutationError,
  });
  const addManualAttachment = async (file: File) => {
    const content_base64 = await fileToBase64(file);
    setManualAttachments((items) => [
      ...items,
      {
        file_name: file.name,
        content_type: file.type || 'application/octet-stream',
        file_size: file.size,
        content_base64,
      },
    ]);
  };

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
      <PageTitle
        title="邮件中心"
        extra={(
          <Space>
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
            <Button icon={<DownloadOutlined />} loading={exportMutation.isPending} onClick={() => exportMutation.mutate()}>
              导出
            </Button>
            <Upload
              accept=".eml,message/rfc822"
              showUploadList={false}
              beforeUpload={(file) => {
                ingestEmlMutation.mutate(file);
                return false;
              }}
            >
              <Button icon={<UploadOutlined />} loading={ingestEmlMutation.isPending}>导入 EML</Button>
            </Upload>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setIngestOpen(true)}>
              手动入库
            </Button>
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
                { value: 'customer_supplement', label: '客户补充' },
                { value: 'normal_reply', label: '普通回复/转发' },
                { value: 'rma_sent', label: 'RMA 已发送' },
                { value: 'device_received', label: '设备已收到' },
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
      <Drawer width={760} title="邮件详情" open={Boolean(selectedId)} onClose={() => setSelectedId(null)}>
        {detailQuery.data ? (
          <div className="drawer-stack">
            <Descriptions column={1} size="small" bordered>
              <Descriptions.Item label="主题">{detailQuery.data.email.subject || '-'}</Descriptions.Item>
              <Descriptions.Item label="发件人"><CopyableField value={detailQuery.data.email.from_address} /></Descriptions.Item>
              <Descriptions.Item label="收件人">{detailQuery.data.email.to_addresses || '-'}</Descriptions.Item>
              <Descriptions.Item label="Message-ID"><CopyableField value={detailQuery.data.email.message_id || ''} displayText={detailQuery.data.email.message_id || '-'} /></Descriptions.Item>
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
      <Modal
        title="手动邮件入库"
        open={ingestOpen}
        onCancel={() => {
          setIngestOpen(false);
          setManualAttachments([]);
        }}
        footer={null}
        destroyOnClose
      >
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
          <Form.Item label="附件">
            <Upload
              multiple
              accept=".docx,.xlsx,.csv,.txt,.html,.htm,.pdf,image/*"
              showUploadList={false}
              beforeUpload={(file) => {
                addManualAttachment(file).catch(handleMutationError);
                return Upload.LIST_IGNORE;
              }}
            >
              <Button icon={<UploadOutlined />}>选择附件</Button>
            </Upload>
            <Table<EmailIngestAttachment>
              size="small"
              rowKey={(record) => `${record.file_name}-${record.file_size ?? 0}`}
              dataSource={manualAttachments}
              pagination={false}
              style={{ marginTop: 8 }}
              columns={[
                { title: '文件名', dataIndex: 'file_name', ellipsis: true },
                { title: '类型', dataIndex: 'content_type', width: 160 },
                { title: '大小', dataIndex: 'file_size', width: 100, render: (value?: number | null) => formatFileSizeKb(undefined, value) },
                {
                  title: '操作',
                  width: 80,
                  render: (_, record) => (
                    <Button
                      type="link"
                      size="small"
                      onClick={() => setManualAttachments((items) => items.filter((item) => item !== record))}
                    >
                      移除
                    </Button>
                  ),
                },
              ]}
            />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={ingestMutation.isPending}>
            入库
          </Button>
        </Form>
      </Modal>
    </div>
  );
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const value = String(reader.result || '');
      resolve(value.includes(',') ? value.split(',', 2)[1] : value);
    };
    reader.onerror = () => reject(reader.error ?? new Error('FILE_READ_FAILED'));
    reader.readAsDataURL(file);
  });
}
