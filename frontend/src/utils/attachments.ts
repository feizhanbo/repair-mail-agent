import type { Attachment } from '../types/api';

export const ARCHIVE_DOWNLOAD_WARNING = '工程辅助压缩附件未经内容扫描，请仅在受控环境中打开。';

const formatLabels: Record<string, string> = {
  zip: 'ZIP',
  rar: 'RAR',
  '7z': '7Z',
  tar: 'TAR',
  tar_gz: 'TAR.GZ',
  gzip: 'GZIP',
};

export function isEngineeringReference(attachment: Attachment): boolean {
  return attachment.extracted_json?.attachment_role === 'engineering_reference';
}

export function archiveFormatLabel(attachment: Attachment): string | null {
  if (!isEngineeringReference(attachment)) return null;
  const value = attachment.extracted_json?.detected_format;
  if (typeof value !== 'string' || !value) return '压缩包';
  return formatLabels[value] ?? value.toUpperCase();
}

export function attachmentTypeLabel(attachment: Attachment): string {
  const archiveFormat = archiveFormatLabel(attachment);
  if (archiveFormat) return `工程辅助资料（${archiveFormat}）`;
  return attachment.is_inline ? '正文嵌入附件' : '普通附件';
}
