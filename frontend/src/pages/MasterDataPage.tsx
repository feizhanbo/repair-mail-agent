import { UploadOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Form, Input, Modal, Space, Table, Tabs, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useMemo, useState } from 'react';
import { api, apiErrorMessage } from '../api/client';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import StatusTag from '../components/StatusTag';
import { useAuthStore } from '../stores/authStore';
import type { BoardCard, SnAsset } from '../types/api';
import { hasRole } from '../utils/roles';

type ImportForm = {
  json: string;
  source_file_name?: string;
};

type ImportKind = 'sn' | 'board';

export default function MasterDataPage() {
  const [activeTab, setActiveTab] = useState<ImportKind>('sn');
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [importOpen, setImportOpen] = useState(false);
  const queryClient = useQueryClient();
  const canImport = hasRole(useAuthStore((state) => state.user?.roles), 'admin');
  const snQuery = useQuery({
    queryKey: ['sn-assets', keyword, page],
    queryFn: () => api.snAssets({ keyword, page, page_size: 20 }),
    enabled: activeTab === 'sn',
  });
  const boardQuery = useQuery({
    queryKey: ['board-cards', keyword, page],
    queryFn: () => api.boardCards({ keyword, page, page_size: 20 }),
    enabled: activeTab === 'board',
  });
  const importMutation = useMutation({
    mutationFn: (values: ImportForm) => {
      const items = JSON.parse(values.json) as unknown[];
      const body = { items, source_file_name: values.source_file_name };
      return activeTab === 'sn' ? api.importSnAssets(body) : api.importBoardCards(body);
    },
    onSuccess: () => {
      message.success('基础资料已导入');
      setImportOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['sn-assets'] });
      void queryClient.invalidateQueries({ queryKey: ['board-cards'] });
    },
    onError: (error) => message.error(error instanceof SyntaxError ? '导入数据格式不正确' : apiErrorMessage(error)),
  });
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
        extra={canImport ? <Button type="primary" icon={<UploadOutlined />} onClick={() => setImportOpen(true)} /> : null}
      />
      <SectionPanel>
        <Space className="filter-bar">
          <Input.Search allowClear placeholder="SN、客户、物料" onSearch={(value) => { setPage(1); setKeyword(value); }} />
        </Space>
        <Tabs
          activeKey={activeTab}
          onChange={(key) => { setActiveTab(key as ImportKind); setPage(1); }}
          items={[
            {
              key: 'sn',
              label: 'SN 资产库',
              children: (
                <Table<SnAsset>
                  rowKey="id"
                  columns={snColumns}
                  dataSource={snQuery.data?.items ?? []}
                  loading={snQuery.isFetching}
                  locale={{ emptyText: snQuery.isError ? 'SN 资产加载失败' : '暂无 SN 资产' }}
                  pagination={{ current: page, pageSize: 20, total: snQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
                />
              ),
            },
            {
              key: 'board',
              label: '板卡规则',
              children: (
                <Table<BoardCard>
                  rowKey="id"
                  columns={boardColumns}
                  dataSource={boardQuery.data?.items ?? []}
                  loading={boardQuery.isFetching}
                  locale={{ emptyText: boardQuery.isError ? '板卡规则加载失败' : '暂无板卡规则' }}
                  pagination={{ current: page, pageSize: 20, total: boardQuery.data?.total ?? 0, onChange: setPage, showSizeChanger: false }}
                />
              ),
            },
          ]}
        />
      </SectionPanel>
      <Modal title={activeTab === 'sn' ? '导入 SN 资产' : '导入板卡规则'} open={importOpen} onCancel={() => setImportOpen(false)} footer={null} destroyOnClose>
        <Form<ImportForm> layout="vertical" onFinish={(values) => importMutation.mutate(values)}>
          <Form.Item label="来源文件名" name="source_file_name">
            <Input />
          </Form.Item>
          <Form.Item label="JSON 数据" name="json" rules={[{ required: true }]}>
            <Input.TextArea rows={10} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={importMutation.isPending}>
            导入
          </Button>
        </Form>
      </Modal>
    </div>
  );
}
