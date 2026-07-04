import type { ReactNode } from 'react';
import { Space, Typography } from 'antd';

type PageTitleProps = {
  title: string;
  extra?: ReactNode;
};

export default function PageTitle({ title, extra }: PageTitleProps) {
  return (
    <div className="page-title-row">
      <Typography.Title level={2}>{title}</Typography.Title>
      {extra ? <Space wrap>{extra}</Space> : null}
    </div>
  );
}
