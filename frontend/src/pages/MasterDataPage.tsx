import { DownloadOutlined, UploadOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Tabs, Upload, message } from 'antd';
import type { UploadProps } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useMemo, useState, type Key } from 'react';
import { api, apiErrorMessage } from '../api/client';
import { waitForJob } from '../utils/jobs';
import ErrorResult from '../components/ErrorResult';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import { useAuthStore } from '../stores/authStore';
import type { BoardCard, CustomerServicePolicy, SnAsset } from '../types/api';
import { compactFilters } from '../utils/filters';
import { saveBlob } from '../utils/download';
import { hasRole } from '../utils/roles';

type ImportKind = 'sn' | 'board' | 'policy';

type SnFilters = {
  sn?: string;
  customer?: string;
  material?: string;
  asset_status?: string;
};

type BoardFilters = {
  material_code?: string;
  material_name?: string;
  status?: string;
};

type PolicyFormValues = Partial<CustomerServicePolicy> & {
  repair_price?: number;
  tax_rate?: number;
};

export default function MasterDataPage() {
  const [activeTab, setActiveTab] = useState<ImportKind>('sn');
  const [page, setPage] = useState(1);
  const [snFilters, setSnFilters] = useState<Record<string, unknown>>({});
  const [boardFilters, setBoardFilters] = useState<Record<string, unknown>>({});
  const [policyFilters, setPolicyFilters] = useState<Record<string, unknown>>({});
  const [editingPolicy, setEditingPolicy] = useState<CustomerServicePolicy | null | undefined>(undefined);
  const [selectedSnKeys, setSelectedSnKeys] = useState<Key[]>([]);
  const [selectedBoardKeys, setSelectedBoardKeys] = useState<Key[]>([]);
  const [snFilterForm] = Form.useForm<SnFilters>();
  const [boardFilterForm] = Form.useForm<BoardFilters>();
  const [policyFilterForm] = Form.useForm<Record<string, unknown>>();
  const [policyForm] = Form.useForm<PolicyFormValues>();
  const queryClient = useQueryClient();
  const canImport = hasRole(useAuthStore((state) => state.user?.roles), 'admin');

  const snQuery = useQuery({
    queryKey: ['sn-assets', snFilters, page],
    queryFn: () => api.snAssets({ ...snFilters, page, page_size: 20 }),
    enabled: activeTab === 'sn',
  });
  const boardQuery = useQuery({
    queryKey: ['board-cards', boardFilters, page],
    queryFn: () => api.boardCards({ ...boardFilters, page, page_size: 20 }),
    enabled: activeTab === 'board',
  });
  const policyQuery = useQuery({
    queryKey: ['customer-policies', policyFilters, page],
    queryFn: () => api.customerPolicies({ ...policyFilters, page, page_size: 20 }),
    enabled: activeTab === 'policy',
  });
  const savePolicyMutation = useMutation({
    mutationFn: (values: Record<string, unknown>) => (
      editingPolicy
        ? api.updateCustomerPolicy(editingPolicy.id, values)
        : api.createCustomerPolicy(values)
    ),
    onSuccess: () => {
      message.success('客户政策已保存');
      setEditingPolicy(undefined);
      policyForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['customer-policies'] });
    },
    onError: (error) => message.error(apiErrorMessage(error)),
  });

  const templateMutation = useMutation({
    mutationFn: () => (activeTab === 'sn' ? api.snAssetsTemplate() : api.boardCardsTemplate()),
    onSuccess: (blob) => saveBlob(blob, activeTab === 'sn' ? 'sn-assets-template.xlsx' : 'board-cards-template.xlsx'),
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const exportMutation = useMutation({
    mutationFn: () => (
      activeTab === 'sn'
        ? api.exportSelectedSnAssets(selectedSnKeys.map(Number))
        : api.exportSelectedBoardCards(selectedBoardKeys.map(Number))
    ),
    onSuccess: (blob) => saveBlob(blob, activeTab === 'sn' ? 'sn-assets-selected-export.xlsx' : 'board-cards-selected-export.xlsx'),
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const importFileMutation = useMutation({
    mutationFn: async (file: File) => {
      if (import.meta.env.VITE_IMPORT_EXPORT_ASYNC_ENABLED !== 'true') {
        return activeTab === 'sn' ? api.importSnAssetsFile(file) : api.importBoardCardsFile(file);
      }
      const job = activeTab === 'sn' ? await api.importSnAssetsFileJob(file) : await api.importBoardCardsFileJob(file);
      return waitForJob(job);
    },
    onSuccess: () => {
      message.success('基础资料已导入');
      void queryClient.invalidateQueries({ queryKey: ['sn-assets'] });
      void queryClient.invalidateQueries({ queryKey: ['board-cards'] });
    },
    onError: (error) => message.error(apiErrorMessage(error)),
  });

  const uploadProps: UploadProps = {
    accept: '.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    beforeUpload: (file) => {
      importFileMutation.mutate(file);
      return Upload.LIST_IGNORE;
    },
    showUploadList: false,
  };

  const snColumns: ColumnsType<SnAsset> = useMemo(
    () => [
      { title: 'SN', dataIndex: 'sn', width: 180 },
      { title: '客户代码', dataIndex: 'customer_code', width: 120 },
      { title: '客户名称', dataIndex: 'customer_name', ellipsis: true },
      { title: '物料编码', dataIndex: 'material_code', width: 140 },
      { title: '物料名称', dataIndex: 'material_name', ellipsis: true, render: (value?: string) => value || '-' },
      { title: '服务追踪卡编号', dataIndex: 'service_tracking_card_no', width: 150, render: (value?: string) => value || '-' },
      { title: '上级 SN', dataIndex: 'parent_sn', width: 160, render: (value?: string) => value || '-' },
      { title: 'Top SN', dataIndex: 'top_sn', width: 160, render: (value?: string) => value || '-' },
      { title: '上级物料代码', dataIndex: 'parent_material_code', width: 150, render: (value?: string) => value || '-' },
      { title: 'Top 物料代码', dataIndex: 'top_material_code', width: 150, render: (value?: string) => value || '-' },
      { title: '状态', dataIndex: 'asset_status', width: 100, render: (value: string) => <StatusTag value={value === 'valid' ? 'pass' : 'warning'} /> },
    ],
    [],
  );
  const boardColumns: ColumnsType<BoardCard> = useMemo(
    () => [
      { title: '物料编码', dataIndex: 'material_code', width: 160 },
      { title: '物料名称', dataIndex: 'material_name', ellipsis: true, render: (value?: string) => value || '-' },
      { title: '寄北京', dataIndex: 'need_ship_to_beijing', width: 100, render: (value: boolean) => <StatusTag value={value ? 'pass' : 'pending'} /> },
      { title: '寄送地址', dataIndex: 'shipping_address', ellipsis: true, render: (value?: string) => value || '-' },
      { title: '状态', dataIndex: 'status', width: 100, render: (value: string) => <StatusTag value={value} /> },
    ],
    [],
  );
  const selectedCount = activeTab === 'sn' ? selectedSnKeys.length : selectedBoardKeys.length;
  const openPolicyEditor = (policy: CustomerServicePolicy | null) => {
    setEditingPolicy(policy);
    policyForm.setFieldsValue(policy ? {
      ...policy,
      repair_price: Number(policy.repair_price),
      tax_rate: Number(policy.tax_rate),
    } : {
      policy_type: 'special_out_of_warranty',
      currency: 'CNY',
      tax_rate: 13,
      shipping_fee_text: 'one-way charge/单次收费',
      enabled: true,
      hide_company_name: false,
      force_manual_review: false,
    });
  };
  const policyColumns: ColumnsType<CustomerServicePolicy> = [
    { title: '客户代码', dataIndex: 'customer_code', width: 130 },
    { title: '客户名称', dataIndex: 'customer_name', ellipsis: true, render: (v?: string) => v || '-' },
    { title: '政策类型', dataIndex: 'policy_type', width: 170 },
    { title: '生效日期', dataIndex: 'effective_from', width: 120, render: (v?: string) => v || '-' },
    { title: '失效日期', dataIndex: 'effective_until', width: 120, render: (v?: string) => v || '-' },
    { title: '维修价', dataIndex: 'repair_price', width: 100 },
    { title: '币种', dataIndex: 'currency', width: 75 },
    { title: '税率', dataIndex: 'tax_rate', width: 75, render: (v: number | string) => `${v}%` },
    { title: '快递费规则', dataIndex: 'shipping_fee_text', width: 180 },
    { title: '称呼', dataIndex: 'reply_salutation', width: 110, render: (v?: string) => v || '-' },
    { title: '隐藏公司名', dataIndex: 'hide_company_name', width: 100, render: (v: boolean) => <StatusTag value={v ? 'pass' : 'pending'} /> },
    { title: '强制复核', dataIndex: 'force_manual_review', width: 90, render: (v: boolean) => <StatusTag value={v ? 'warning' : 'pass'} /> },
    { title: '启用', dataIndex: 'enabled', width: 75, render: (v: boolean) => <StatusTag value={v ? 'active' : 'disabled'} /> },
    { title: '操作', width: 80, fixed: 'right', render: (_: unknown, row) => <Button type="link" size="small" onClick={() => openPolicyEditor(row)}>编辑</Button> },
  ];

  return (
    <div className="page-stack">
      <PageTitle
        title="基础资料"
        extra={(
          <Space wrap>
            {activeTab === 'policy' && canImport ? <Button type="primary" onClick={() => openPolicyEditor(null)}>新增政策</Button> : null}
            {activeTab !== 'policy' ? (
              <>
            <Button icon={<DownloadOutlined />} loading={templateMutation.isPending} onClick={() => templateMutation.mutate()}>
              模板
            </Button>
            <Button
              icon={<DownloadOutlined />}
              loading={exportMutation.isPending}
              disabled={selectedCount === 0}
              onClick={() => exportMutation.mutate()}
            >
              导出已选{selectedCount ? `(${selectedCount})` : ''}
            </Button>
            {canImport ? (
              <Upload {...uploadProps}>
                <Button type="primary" icon={<UploadOutlined />} loading={importFileMutation.isPending}>
                  导入
                </Button>
              </Upload>
            ) : null}
              </>
            ) : null}
          </Space>
        )}
      />
      <SectionPanel>
        <Tabs
          activeKey={activeTab}
          onChange={(key) => {
            setActiveTab(key as ImportKind);
            setPage(1);
            setSelectedSnKeys([]);
            setSelectedBoardKeys([]);
          }}
          items={[
            {
              key: 'sn',
              label: 'SN 资产库',
              children: (
                <div className="page-stack">
                  <Form<SnFilters>
                    form={snFilterForm}
                    layout="inline"
                    className="filter-bar"
                    onFinish={(values) => {
                      setPage(1);
                      setSnFilters(compactFilters(values));
                      setSelectedSnKeys([]);
                    }}
                  >
                    <Form.Item name="sn">
                      <Input allowClear placeholder="SN" />
                    </Form.Item>
                    <Form.Item name="customer">
                      <Input allowClear placeholder="客户" />
                    </Form.Item>
                    <Form.Item name="material">
                      <Input allowClear placeholder="物料" />
                    </Form.Item>
                    <Form.Item name="asset_status">
                      <Select
                        allowClear
                        placeholder="状态"
                        style={{ width: 120 }}
                        options={[{ value: 'valid', label: '有效' }, { value: 'invalid', label: '无效' }]}
                      />
                    </Form.Item>
                    <Space>
                      <Button htmlType="submit" type="primary">筛选</Button>
                      <Button
                        onClick={() => {
                          snFilterForm.resetFields();
                          setPage(1);
                          setSnFilters({});
                          setSelectedSnKeys([]);
                        }}
                      >
                        重置
                      </Button>
                    </Space>
                  </Form>
                  <Table<SnAsset>
                    rowKey="id"
                    columns={snColumns}
                    dataSource={snQuery.data?.items ?? []}
                    loading={snQuery.isFetching}
                    rowSelection={{
                      selectedRowKeys: selectedSnKeys,
                      onChange: setSelectedSnKeys,
                    }}
                    locale={{
                      emptyText: snQuery.isError
                        ? <ErrorResult message={apiErrorMessage(snQuery.error)} onRetry={() => snQuery.refetch()} />
                        : '暂无 SN 资产'
                    }}
                    pagination={{
                      current: page,
                      pageSize: 20,
                      total: snQuery.data?.total ?? 0,
                      onChange: (nextPage) => {
                        setPage(nextPage);
                        setSelectedSnKeys([]);
                      },
                      showSizeChanger: false,
                    }}
                  />
                </div>
              ),
            },
            {
              key: 'board',
              label: '板卡规则',
              children: (
                <div className="page-stack">
                  <Form<BoardFilters>
                    form={boardFilterForm}
                    layout="inline"
                    className="filter-bar"
                    onFinish={(values) => {
                      setPage(1);
                      setBoardFilters(compactFilters(values));
                      setSelectedBoardKeys([]);
                    }}
                  >
                    <Form.Item name="material_code">
                      <Input allowClear placeholder="物料编码" />
                    </Form.Item>
                    <Form.Item name="material_name">
                      <Input allowClear placeholder="物料名称" />
                    </Form.Item>
                    <Form.Item name="status">
                      <Select
                        allowClear
                        placeholder="状态"
                        style={{ width: 120 }}
                        options={[{ value: 'active', label: '启用' }, { value: 'disabled', label: '停用' }]}
                      />
                    </Form.Item>
                    <Space>
                      <Button htmlType="submit" type="primary">筛选</Button>
                      <Button
                        onClick={() => {
                          boardFilterForm.resetFields();
                          setPage(1);
                          setBoardFilters({});
                          setSelectedBoardKeys([]);
                        }}
                      >
                        重置
                      </Button>
                    </Space>
                  </Form>
                  <Table<BoardCard>
                    rowKey="id"
                    columns={boardColumns}
                    dataSource={boardQuery.data?.items ?? []}
                    loading={boardQuery.isFetching}
                    rowSelection={{
                      selectedRowKeys: selectedBoardKeys,
                      onChange: setSelectedBoardKeys,
                    }}
                    locale={{
                      emptyText: boardQuery.isError
                        ? <ErrorResult message={apiErrorMessage(boardQuery.error)} onRetry={() => boardQuery.refetch()} />
                        : '暂无板卡规则'
                    }}
                    pagination={{
                      current: page,
                      pageSize: 20,
                      total: boardQuery.data?.total ?? 0,
                      onChange: (nextPage) => {
                        setPage(nextPage);
                        setSelectedBoardKeys([]);
                      },
                      showSizeChanger: false,
                    }}
                  />
                </div>
              ),
            },
            {
              key: 'policy',
              label: '客户服务政策',
              children: (
                <div className="page-stack">
                  <Form
                    form={policyFilterForm}
                    layout="inline"
                    className="filter-bar"
                    onFinish={(values) => {
                      setPage(1);
                      setPolicyFilters(compactFilters(values));
                    }}
                  >
                    <Form.Item name="customer_code"><Input allowClear placeholder="客户代码" /></Form.Item>
                    <Form.Item name="policy_type">
                      <Select
                        allowClear
                        placeholder="政策类型"
                        style={{ width: 190 }}
                        options={[
                          { value: 'default', label: '默认超保价' },
                          { value: 'permanent_free', label: '永久免费' },
                          { value: 'annual_free', label: '包年免费' },
                          { value: 'special_out_of_warranty', label: '特殊超保价' },
                        ]}
                      />
                    </Form.Item>
                    <Form.Item name="enabled">
                      <Select
                        allowClear
                        placeholder="启用状态"
                        style={{ width: 120 }}
                        options={[{ value: true, label: '启用' }, { value: false, label: '停用' }]}
                      />
                    </Form.Item>
                    <Space>
                      <Button htmlType="submit" type="primary">筛选</Button>
                      <Button onClick={() => {
                        policyFilterForm.resetFields();
                        setPage(1);
                        setPolicyFilters({});
                      }}>重置</Button>
                    </Space>
                  </Form>
                  <Table<CustomerServicePolicy>
                    rowKey="id"
                    columns={policyColumns}
                    dataSource={policyQuery.data?.items ?? []}
                    loading={policyQuery.isFetching}
                    scroll={{ x: 1550 }}
                    locale={{
                      emptyText: policyQuery.isError
                        ? <ErrorResult message={apiErrorMessage(policyQuery.error)} onRetry={() => policyQuery.refetch()} />
                        : '暂无客户政策',
                    }}
                    pagination={{
                      current: page,
                      pageSize: 20,
                      total: policyQuery.data?.total ?? 0,
                      onChange: setPage,
                      showSizeChanger: false,
                    }}
                  />
                </div>
              ),
            },
          ]}
        />
      </SectionPanel>
      <Modal
        title={editingPolicy ? '编辑客户政策' : '新增客户政策'}
        open={editingPolicy !== undefined}
        onCancel={() => setEditingPolicy(undefined)}
        footer={null}
        destroyOnClose
      >
        <Form form={policyForm} layout="vertical" onFinish={(values) => savePolicyMutation.mutate({ ...values })}>
          <Space style={{ display: 'flex' }} align="start">
            <Form.Item label="政策编码" name="policy_code" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item label="客户代码" name="customer_code" rules={[{ required: true }]}><Input disabled={Boolean(editingPolicy)} /></Form.Item>
          </Space>
          <Form.Item label="客户名称" name="customer_name"><Input /></Form.Item>
          <Form.Item label="政策类型" name="policy_type" rules={[{ required: true }]}>
            <Select options={[
              { value: 'default', label: '默认超保价' },
              { value: 'permanent_free', label: '永久免费' },
              { value: 'annual_free', label: '包年免费' },
              { value: 'special_out_of_warranty', label: '特殊超保价' },
            ]} />
          </Form.Item>
          <Space style={{ display: 'flex' }} align="start">
            <Form.Item label="生效日期" name="effective_from"><Input type="date" /></Form.Item>
            <Form.Item label="失效日期" name="effective_until"><Input type="date" /></Form.Item>
          </Space>
          <Space style={{ display: 'flex' }} align="start">
            <Form.Item label="维修价格" name="repair_price" rules={[{ required: true }]}><InputNumber min={0} precision={2} /></Form.Item>
            <Form.Item label="币种" name="currency" rules={[{ required: true }]}><Input style={{ width: 100 }} /></Form.Item>
            <Form.Item label="税率(%)" name="tax_rate" rules={[{ required: true }]}><InputNumber min={0} max={100} precision={4} /></Form.Item>
          </Space>
          <Form.Item label="快递费规则" name="shipping_fee_text" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item label="特殊称呼" name="reply_salutation"><Input /></Form.Item>
          <Space size="large">
            <Form.Item label="隐藏公司名称" name="hide_company_name" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item label="强制人工复核" name="force_manual_review" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item label="启用" name="enabled" valuePropName="checked"><Switch /></Form.Item>
          </Space>
          <Button type="primary" htmlType="submit" loading={savePolicyMutation.isPending}>保存</Button>
        </Form>
      </Modal>
    </div>
  );
}
