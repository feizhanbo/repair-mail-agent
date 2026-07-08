import { DownloadOutlined, UploadOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Form, Input, Select, Space, Table, Tabs, Upload, message } from 'antd';
import type { UploadProps } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useMemo, useState } from 'react';
import { api, apiErrorMessage } from '../api/client';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import { useAuthStore } from '../stores/authStore';
import type { BoardCard, SnAsset } from '../types/api';
import { compactFilters } from '../utils/filters';
import { saveBlob } from '../utils/download';
import { hasRole } from '../utils/roles';

type ImportKind = 'sn' | 'board';

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

export default function MasterDataPage() {
  const [activeTab, setActiveTab] = useState<ImportKind>('sn');
  const [page, setPage] = useState(1);
  const [snFilters, setSnFilters] = useState<Record<string, unknown>>({});
  const [boardFilters, setBoardFilters] = useState<Record<string, unknown>>({});
  const [snFilterForm] = Form.useForm<SnFilters>();
  const [boardFilterForm] = Form.useForm<BoardFilters>();
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

  const templateMutation = useMutation({
    mutationFn: () => (activeTab === 'sn' ? api.snAssetsTemplate() : api.boardCardsTemplate()),
    onSuccess: (blob) => saveBlob(blob, activeTab === 'sn' ? 'sn-assets-template.xlsx' : 'board-cards-template.xlsx'),
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const exportMutation = useMutation({
    mutationFn: () => (
      activeTab === 'sn'
        ? api.exportSnAssets(snFilters)
        : api.exportBoardCards(boardFilters)
    ),
    onSuccess: (blob) => saveBlob(blob, activeTab === 'sn' ? 'sn-assets-export.xlsx' : 'board-cards-export.xlsx'),
    onError: (error) => message.error(apiErrorMessage(error)),
  });
  const importFileMutation = useMutation({
    mutationFn: (file: File) => (activeTab === 'sn' ? api.importSnAssetsFile(file) : api.importBoardCardsFile(file)),
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

  return (
    <div className="page-stack">
      <PageTitle
        title="基础资料"
        extra={(
          <Space wrap>
            <Button icon={<DownloadOutlined />} loading={templateMutation.isPending} onClick={() => templateMutation.mutate()}>
              模板
            </Button>
            <Button icon={<DownloadOutlined />} loading={exportMutation.isPending} onClick={() => exportMutation.mutate()}>
              导出
            </Button>
            {canImport ? (
              <Upload {...uploadProps}>
                <Button type="primary" icon={<UploadOutlined />} loading={importFileMutation.isPending}>
                  导入
                </Button>
              </Upload>
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
                    locale={{ emptyText: snQuery.isError ? 'SN 资产加载失败' : '暂无 SN 资产' }}
                    pagination={{ current: page, pageSize: 20, total: snQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
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
                    locale={{ emptyText: boardQuery.isError ? '板卡规则加载失败' : '暂无板卡规则' }}
                    pagination={{ current: page, pageSize: 20, total: boardQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
                  />
                </div>
              ),
            },
          ]}
        />
      </SectionPanel>
    </div>
  );
}
