import { TableOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Collapse, Empty, Table, Tabs, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../api/client';
import PageTitle from '../components/PageTitle';
import SectionPanel from '../components/SectionPanel';
import type { DatabaseColumnInfo, DatabaseTableInfo, JsonRecord } from '../types/api';

const fieldColumns: ColumnsType<DatabaseColumnInfo> = [
  { title: '字段名', dataIndex: 'name', key: 'name', width: 200 },
  { title: '类型', dataIndex: 'type', key: 'type', width: 180 },
  {
    title: '可空',
    dataIndex: 'nullable',
    key: 'nullable',
    width: 80,
    render: (value: boolean) => <Tag color={value ? 'orange' : 'blue'}>{value ? 'YES' : 'NO'}</Tag>,
  },
  {
    title: '默认值',
    dataIndex: 'default',
    key: 'default',
    width: 140,
    render: (value: string | null | undefined) =>
      value != null ? <Typography.Text code>{value}</Typography.Text> : <Typography.Text type="secondary">-</Typography.Text>,
  },
  { title: '注释', dataIndex: 'comment', key: 'comment' },
];

function renderCellValue(value: unknown) {
  if (value == null) {
    return <Typography.Text type="secondary">NULL</Typography.Text>;
  }
  if (typeof value === 'object') {
    return <Typography.Text code>{JSON.stringify(value)}</Typography.Text>;
  }
  return <Typography.Text>{String(value)}</Typography.Text>;
}

function DataTab({ tableName }: { tableName: string }) {
  const rowsQuery = useQuery({
    queryKey: ['db-browser-rows', tableName],
    queryFn: () => api.dbRows(tableName, { page: 1, page_size: 100 }),
  });
  const data = rowsQuery.data;
  const dataColumns: ColumnsType<JsonRecord> =
    data?.columns.map((column) => ({
      title: column,
      dataIndex: column,
      key: column,
      width: 180,
      ellipsis: true,
      render: renderCellValue,
    })) ?? [];

  if (!rowsQuery.isFetching && data?.rows.length === 0) {
    return <Empty description="暂无数据" />;
  }

  return (
    <div className="page-stack">
      <Typography.Text type="secondary">
        共 {data?.total ?? 0} 行，当前显示前 {Math.min(data?.total ?? 0, data?.page_size ?? 100)} 行
      </Typography.Text>
      <Table<JsonRecord>
        size="small"
        rowKey={(_, index) => `${tableName}-${index ?? 0}`}
        dataSource={data?.rows ?? []}
        columns={dataColumns}
        pagination={false}
        scroll={{ x: 'max-content' }}
        loading={rowsQuery.isFetching}
      />
    </div>
  );
}

function tableLabel(table: DatabaseTableInfo) {
  return (
    <span>
      <Typography.Text strong>{table.table_name}</Typography.Text>
      {table.table_comment ? (
        <Typography.Text type="secondary" style={{ marginLeft: 12 }}>
          {table.table_comment}
        </Typography.Text>
      ) : null}
      <Tag style={{ marginLeft: 12 }}>{table.columns.length} 字段</Tag>
    </span>
  );
}

export default function DbBrowser() {
  const tablesQuery = useQuery({
    queryKey: ['db-browser-tables'],
    queryFn: api.dbTables,
  });
  const tables = tablesQuery.data?.tables ?? [];

  return (
    <div className="page-stack">
      <PageTitle
        title="数据库浏览器"
        extra={
          <Typography.Text type="secondary">
            <TableOutlined /> 只读查看当前业务库表结构和样例数据
          </Typography.Text>
        }
      />
      <SectionPanel>
        {tables.length === 0 && !tablesQuery.isFetching ? (
          <Empty description="暂无数据表" />
        ) : (
          <Collapse
            size="small"
            items={tables.map((table) => ({
              key: table.table_name,
              label: tableLabel(table),
              children: (
                <Tabs
                  defaultActiveKey="fields"
                  size="small"
                  items={[
                    {
                      key: 'fields',
                      label: '字段',
                      children: (
                        <Table<DatabaseColumnInfo>
                          size="small"
                          rowKey="name"
                          dataSource={table.columns}
                          columns={fieldColumns}
                          pagination={false}
                        />
                      ),
                    },
                    {
                      key: 'data',
                      label: '数据',
                      children: <DataTab tableName={table.table_name} />,
                    },
                  ]}
                />
              ),
            }))}
          />
        )}
      </SectionPanel>
    </div>
  );
}
