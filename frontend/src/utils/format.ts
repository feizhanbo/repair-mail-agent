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
