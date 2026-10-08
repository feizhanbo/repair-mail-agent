import { useQuery } from '@tanstack/react-query';
import { Button, Form, Input, Space, Table, Tabs, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api, apiErrorMessage } from '../api/client';
import ErrorResult from '../components/ErrorResult';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import type { BoardCard, CustomerServicePolicy, SnAsset } from '../types/api';

type TabKey = 'policy' | 'sn' | 'board';

export default function MasterDataPage() {
  const [activeTab, setActiveTab] = useState<TabKey>('policy');
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [form] = Form.useForm<{ keyword?: string }>();
  const params = { page, page_size: 20, keyword: keyword || undefined };
  const policyQuery = useQuery({ queryKey: ['customer-policies', params], queryFn: () => api.customerPolicies(params), enabled: activeTab === 'policy' });
  const snQuery = useQuery({ queryKey: ['sn-assets', params], queryFn: () => api.snAssets(params), enabled: activeTab === 'sn' });
  const boardQuery = useQuery({ queryKey: ['board-cards', params], queryFn: () => api.boardCards(params), enabled: activeTab === 'board' });

  const policyColumns: ColumnsType<CustomerServicePolicy> = [
    { title: '客户代码', dataIndex: 'customer_code', width: 130 },
    { title: '客户名称', dataIndex: 'customer_name', ellipsis: true, render: (v?: string) => v || '-' },
    { title: '政策类型', dataIndex: 'policy_type', width: 180 },
    { title: '收费状态', dataIndex: 'charge_status', width: 150 },
    { title: '范围', dataIndex: 'customer_scope', width: 90, render: (v?: string) => v === 'overseas' ? '海外' : v === 'domestic' ? '国内' : '待确认' },
    { title: '生效日期', dataIndex: 'effective_from', width: 120, render: (v?: string) => v || '-' },
    { title: '失效日期', dataIndex: 'effective_until', width: 120, render: (v?: string) => v || '-' },
    { title: '维修单价', dataIndex: 'repair_price', width: 110 },
    { title: '币种', dataIndex: 'currency', width: 80 },
    { title: '启用', dataIndex: 'enabled', width: 80, render: (v: boolean) => <Tag color={v ? 'green' : 'default'}>{v ? '是' : '否'}</Tag> },
  ];
  const snColumns: ColumnsType<SnAsset> = [
    { title: 'SN', dataIndex: 'sn', width: 180 },
    { title: '客户代码', dataIndex: 'customer_code', width: 120 },
    { title: '客户名称', dataIndex: 'customer_name', ellipsis: true },
    { title: '物料编码', dataIndex: 'material_code', width: 150 },
    { title: '物料名称', dataIndex: 'material_name', ellipsis: true, render: (v?: string) => v || '-' },
    { title: '服务追踪卡', dataIndex: 'service_tracking_card_no', width: 160, render: (v?: string) => v || '-' },
    { title: '上级 SN', dataIndex: 'parent_sn', width: 160, render: (v?: string) => v || '-' },
    { title: 'Top SN', dataIndex: 'top_sn', width: 160, render: (v?: string) => v || '-' },
    { title: '状态', dataIndex: 'asset_status', width: 100 },
    { title: '质保截止', dataIndex: 'warranty_end_date', width: 120, render: (v?: string) => v || '-' },
    { title: '来源', dataIndex: 'source_system', width: 100, render: (v?: string) => v || '-' },
  ];
  const boardColumns: ColumnsType<BoardCard> = [
    { title: '板卡型号', dataIndex: 'board_code', width: 160 },
    { title: '板卡名称', dataIndex: 'board_name', ellipsis: true, render: (v?: string) => v || '-' },
    { title: '客户范围', dataIndex: 'customer_scope', width: 110, render: (v: string) => v === 'overseas' ? '海外' : '国内' },
    { title: '规则类型', dataIndex: 'route_type', width: 130 },
    { title: '寄回地点', dataIndex: 'return_location', width: 110, render: (v: string) => v === 'beijing' ? '北京' : '天津' },
    { title: '维修寄回地址', dataIndex: 'shipping_address', ellipsis: true, render: (v?: string) => v || '-' },
    { title: '联系人', dataIndex: 'shipping_contact', width: 120, render: (v?: string) => v || '-' },
    { title: '电话', dataIndex: 'shipping_phone', width: 150, render: (v?: string) => v || '-' },
    { title: '状态', dataIndex: 'status', width: 100 },
  ];

  const current = activeTab === 'policy' ? policyQuery : activeTab === 'sn' ? snQuery : boardQuery;
  return (
    <div className="page-stack">
      <PageTitle title="基础资料" />
      <SectionPanel>
        <Typography.Paragraph type="secondary">该页面仅供管理员查询。资料由外部主数据及后台自动同步维护，不提供编辑、删除、导入、导出或下载操作。</Typography.Paragraph>
        <Form form={form} layout="inline" onFinish={(values) => { setPage(1); setKeyword(values.keyword?.trim() ?? ''); }}>
          <Form.Item name="keyword"><Input allowClear placeholder="关键字" style={{ width: 260 }} /></Form.Item>
          <Space>
            <Button type="primary" htmlType="submit">查询</Button>
            <Button onClick={() => { form.resetFields(); setPage(1); setKeyword(''); }}>重置</Button>
          </Space>
        </Form>
      </SectionPanel>
      <SectionPanel>
        <Tabs activeKey={activeTab} onChange={(key) => { setActiveTab(key as TabKey); setPage(1); }} items={[
          { key: 'policy', label: '客户政策', children: <Table<CustomerServicePolicy> rowKey="id" columns={policyColumns} dataSource={policyQuery.data?.items ?? []} loading={policyQuery.isFetching} scroll={{ x: 1200 }} pagination={{ current: page, pageSize: 20, total: policyQuery.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} /> },
          { key: 'sn', label: 'SN 资料', children: <Table<SnAsset> rowKey="id" columns={snColumns} dataSource={snQuery.data?.items ?? []} loading={snQuery.isFetching} scroll={{ x: 1500 }} pagination={{ current: page, pageSize: 20, total: snQuery.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} /> },
          { key: 'board', label: '板卡规则', children: <Table<BoardCard> rowKey="id" columns={boardColumns} dataSource={boardQuery.data?.items ?? []} loading={boardQuery.isFetching} scroll={{ x: 1200 }} pagination={{ current: page, pageSize: 20, total: boardQuery.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} /> },
        ]} />
        {current.isError ? <ErrorResult message={apiErrorMessage(current.error)} onRetry={() => current.refetch()} /> : null}
      </SectionPanel>
    </div>
  );
}
