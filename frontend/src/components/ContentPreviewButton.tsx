import { EyeOutlined, FullscreenOutlined, ZoomInOutlined, ZoomOutOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Button, Empty, Modal, Space, Spin, Typography } from 'antd';
import { useState } from 'react';
import { api } from '../api/client';
import type { ContentPreview } from '../types/api';
import JsonBlock from './JsonBlock';

type Props = {
  kind: 'email' | 'attachment';
  id: number;
  disabled?: boolean;
};

function PreviewContent({ preview }: { preview: ContentPreview }) {
  const [zoom, setZoom] = useState(1);
  const controls = (
    <Space size={4} style={{ marginBottom: 8 }}>
      <Button size="small" icon={<ZoomOutOutlined />} onClick={() => setZoom((value) => Math.max(0.25, Number((value - 0.25).toFixed(2))))} title="缩小" />
      <Button size="small" icon={<FullscreenOutlined />} onClick={() => setZoom(1)} title="适应窗口" />
      <Button size="small" icon={<ZoomInOutlined />} onClick={() => setZoom((value) => Math.min(3, Number((value + 0.25).toFixed(2))))} title="放大" />
      <Typography.Text type="secondary">{Math.round(zoom * 100)}%</Typography.Text>
    </Space>
  );
  if (preview.mode === 'image' && preview.url) {
    return (
      <div>
        {controls}
        <div style={{ maxHeight: '65vh', overflow: 'auto', textAlign: 'center' }}>
          <img src={preview.url} alt={preview.file_name || '附件预览'} style={{ display: 'inline-block', maxWidth: zoom === 1 ? '100%' : 'none', width: `${zoom * 100}%` }} />
        </div>
      </div>
    );
  }
  if (preview.mode === 'pdf' && preview.url) {
    return <iframe title={preview.file_name || 'PDF 预览'} src={preview.url} sandbox="allow-same-origin allow-downloads" style={{ width: '100%', height: '65vh', border: 0 }} />;
  }
  if (preview.mode === 'pdf_pages') {
    return (
      <div>
        {controls}
        <div style={{ maxHeight: '68vh', overflow: 'auto', background: '#f3f4f6', padding: 12 }}>
          {(preview.pages || []).map((page, index) => (
            <figure key={index} style={{ margin: '0 0 12px', textAlign: 'center' }}>
              <img src={page} alt={`PDF 第 ${index + 1} 页`} style={{ display: 'inline-block', width: `${zoom * 100}%`, maxWidth: zoom === 1 ? '100%' : 'none', background: '#fff' }} />
              <figcaption style={{ paddingTop: 4, textAlign: 'center' }}>第 {index + 1} 页</figcaption>
            </figure>
          ))}
          {preview.truncated ? <Typography.Text type="secondary">预览前 {preview.pages?.length || 0} 页，共 {preview.page_count} 页</Typography.Text> : null}
        </div>
      </div>
    );
  }
  if (preview.mode === 'html') {
    return <iframe title={preview.file_name || '邮件预览'} srcDoc={preview.html || ''} sandbox="" style={{ width: '100%', height: '60vh', border: '1px solid #d9d9d9' }} />;
  }
  if (preview.mode === 'extracted') {
    return (
      <div>
        <pre className="json-block" style={{ maxHeight: '45vh' }}>{preview.text || '暂无可预览的提取文本'}</pre>
        <Typography.Title level={5}>结构化结果</Typography.Title>
        <JsonBlock value={preview.extracted_json} />
      </div>
    );
  }
  return preview.text ? <pre className="json-block" style={{ maxHeight: '65vh' }}>{preview.text}</pre> : <Empty description="暂无可预览内容" />;
}

export default function ContentPreviewButton({ kind, id, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const query = useQuery({
    queryKey: ['content-preview', kind, id],
    queryFn: () => kind === 'email' ? api.emailPreview(id) : api.attachmentPreview(id),
    enabled: open,
  });
  return (
    <>
      <Button type="link" size="small" icon={<EyeOutlined />} disabled={disabled} onClick={() => setOpen(true)} title="预览" />
      <Modal width={900} title={query.data?.file_name || (kind === 'email' ? '邮件正文预览' : '附件预览')} open={open} onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        {query.isFetching ? <Spin /> : query.data ? <PreviewContent preview={query.data} /> : <Empty description="预览加载失败" />}
      </Modal>
    </>
  );
}
