import dayjs from 'dayjs';

export function formatTime(value?: string | null) {
  return value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '-';
}

export function compactText(value?: string | null, fallback = '-') {
  if (!value) {
    return fallback;
  }
  return value.length > 80 ? `${value.slice(0, 80)}...` : value;
}

export function numberText(value?: number | string | null) {
  if (value === undefined || value === null || value === '') {
    return '-';
  }
  return String(value);
}

export function fileSizeKbFromBytes(value?: number | null) {
  if (value === undefined || value === null) {
    return null;
  }
  return Math.max(1, Math.ceil(value / 1024));
}

export function formatFileSizeKb(fileSizeKb?: number | null, fileSizeBytes?: number | null) {
  const value = fileSizeKb ?? fileSizeKbFromBytes(fileSizeBytes);
  return value === undefined || value === null ? '-' : `${value} KB`;
}
