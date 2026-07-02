import { createBrowserRouter, Navigate } from 'react-router-dom';
import AppLayout from '../layouts/AppLayout';
import Dashboard from '../pages/Dashboard';
import Login from '../pages/Login';
import PlaceholderPage from '../pages/PlaceholderPage';

const router = createBrowserRouter([
  {
    path: '/login',
    element: <Login />,
  },
  {
    path: '/',
    element: <AppLayout />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: 'emails', element: <PlaceholderPage title="邮件中心" /> },
      { path: 'tickets', element: <PlaceholderPage title="工单中心" /> },
      { path: 'manual-review', element: <PlaceholderPage title="人工复核" /> },
      { path: 'replies', element: <PlaceholderPage title="自动回复审核" /> },
      { path: 'master-data', element: <PlaceholderPage title="基础资料" /> },
      { path: 'ai-logs', element: <PlaceholderPage title="AI 日志" /> },
      { path: 'system', element: <PlaceholderPage title="系统配置" /> },
    ],
  },
  {
    path: '*',
    element: <Navigate to="/" replace />,
  },
]);

export default router;

