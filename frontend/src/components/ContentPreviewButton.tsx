import { EyeOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Button, Empty, Modal, Spin, Typography } from 'antd';
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
  if (preview.mode === 'image' && preview.url) {
    return <img src={preview.url} alt={preview.file_name || '附件预览'} style={{ display: 'block', maxHeight: '65vh', maxWidth: '100%', margin: '0 auto' }} />;
  }
  if (preview.mode === 'pdf' && preview.url) {
    return <iframe title={preview.file_name || 'PDF 预览'} src={preview.url} sandbox="allow-same-origin allow-downloads" style={{ width: '100%', height: '65vh', border: 0 }} />;
  }
  if (preview.mode === 'pdf_pages') {
    return (
      <div style={{ maxHeight: '68vh', overflow: 'auto', background: '#f3f4f6', padding: 12 }}>
        {(preview.pages || []).map((page, index) => (
          <figure key={index} style={{ margin: '0 0 12px' }}>
            <img src={page} alt={`PDF 第 ${index + 1} 页`} style={{ display: 'block', width: '100%', background: '#fff' }} />
            <figcaption style={{ paddingTop: 4, textAlign: 'center' }}>第 {index + 1} 页</figcaption>
          </figure>
        ))}
        {preview.truncated ? <Typography.Text type="secondary">预览前 {preview.pages?.length || 0} 页，共 {preview.page_count} 页</Typography.Text> : null}
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
