import { Card, Col, Row, Table, Typography } from 'antd';

const metrics = [
  ['今日新邮件', 0],
  ['待解析', 0],
  ['待人工', 0],
  ['追问中', 0],
  ['异常', 0],
  ['可导出', 0],
  ['AI 低置信度', 0],
] as const;

export default function Dashboard() {
  return (
    <div className="page-stack">
      <Typography.Title level={2}>首页看板</Typography.Title>
      <Row gutter={[16, 16]}>
        {metrics.map(([label, value]) => (
          <Col key={label} xs={24} sm={12} md={8} xl={6}>
            <Card size="small">
              <Typography.Text type="secondary">{label}</Typography.Text>
              <div className="metric-value">{value}</div>
            </Card>
          </Col>
        ))}
      </Row>
      <Card title="最近异常任务" size="small">
        <Table
          size="middle"
          rowKey="id"
          dataSource={[]}
          columns={[
            { title: '任务', dataIndex: 'job_name' },
            { title: '状态', dataIndex: 'status' },
            { title: '时间', dataIndex: 'created_at' },
          ]}
          pagination={false}
        />
      </Card>
    </div>
  );
}

