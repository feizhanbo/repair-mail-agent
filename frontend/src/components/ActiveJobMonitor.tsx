import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { message } from 'antd';
import { api } from '../api/client';
import { activeJobIds, forgetJob } from '../utils/jobs';

const TERMINAL = new Set(['success', 'needs_manual_review', 'failed', 'cancelled']);

export function ActiveJobMonitor() {
  const queryClient = useQueryClient();

  useEffect(() => {
    const poll = async () => {
      const ids = activeJobIds();
      const jobs = await Promise.all(ids.map((id) => api.job(id).catch(() => null)));
      if (jobs.some((job) => job && TERMINAL.has(job.status))) {
        jobs.forEach((job) => {
          if (!job || !TERMINAL.has(job.status)) return;
          forgetJob(job.id);
          if (job.status === 'success') {
            message.success(`后台任务 #${job.id} 已完成`);
          } else {
            message.error(`后台任务 #${job.id} 处理失败：${job.error_code ?? job.status}`);
          }
        });
        await queryClient.invalidateQueries();
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 3000);
    return () => window.clearInterval(timer);
  }, [queryClient]);

  return null;
}
