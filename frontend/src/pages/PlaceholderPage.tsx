import { Card, Empty, Typography } from 'antd';

type PlaceholderPageProps = {
  title: string;
};

export default function PlaceholderPage({ title }: PlaceholderPageProps) {
  return (
    <div className="page-stack">
      <Typography.Title level={2}>{title}</Typography.Title>
      <Card size="small">
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="模块骨架已创建" />
      </Card>
    </div>
  );
}

