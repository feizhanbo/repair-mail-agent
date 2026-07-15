import { api } from '../api/client';
import type { JobRunLog } from '../types/api';

const STORAGE_KEY = 'repair-mail-active-jobs';
const TERMINAL = new Set(['success', 'needs_manual_review', 'failed', 'cancelled']);

export function activeJobIds(): number[] {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]');
    return Array.isArray(value) ? value.filter((item): item is number => Number.isInteger(item)) : [];
  } catch {
    return [];
  }
}

export function rememberJob(job: JobRunLog): void {
  if (TERMINAL.has(job.status)) return;
  localStorage.setItem(STORAGE_KEY, JSON.stringify([...new Set([...activeJobIds(), job.id])]));
}

export function forgetJob(jobId: number): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(activeJobIds().filter((id) => id !== jobId)));
}

export async function waitForJob(job: JobRunLog, timeoutMs = 30 * 60 * 1000): Promise<JobRunLog> {
  rememberJob(job);
  const started = Date.now();
  let current = job;
  while (!TERMINAL.has(current.status)) {
    if (Date.now() - started > timeoutMs) throw new Error('JOB_POLL_TIMEOUT');
    await new Promise((resolve) => window.setTimeout(resolve, 3000));
    current = await api.job(job.id);
  }
  forgetJob(job.id);
  if (current.status !== 'success') throw new Error(current.error_code ?? current.status);
  return current;
}
