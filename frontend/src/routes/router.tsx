import { createBrowserRouter, Navigate } from 'react-router-dom';
import type { ReactNode } from 'react';
import AppLayout from '../layouts/AppLayout';
import AiLogsPage from '../pages/AiLogsPage';
import Dashboard from '../pages/Dashboard';
import EmailsPage from '../pages/EmailsPage';
import Login from '../pages/Login';
import ManualReviewPage from '../pages/ManualReviewPage';
import MasterDataPage from '../pages/MasterDataPage';
import NotificationCenterPage from '../pages/NotificationCenterPage';
import NotificationsPage from '../pages/NotificationsPage';
import ProfilePage from '../pages/ProfilePage';
import RepliesPage from '../pages/RepliesPage';
import StatisticsPage from '../pages/StatisticsPage';
import SystemPage from '../pages/SystemPage';
import TicketsPage from '../pages/TicketsPage';
import UsersPage from '../pages/UsersPage';
import { useAuthStore } from '../stores/authStore';

function AdminOnly({ children }: { children: ReactNode }) {
  const roles = useAuthStore((state) => state.user?.roles);
  return roles?.includes('admin') ? children : <Navigate to="/" replace />;
}

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
      { path: 'emails', element: <EmailsPage /> },
      { path: 'tickets', element: <TicketsPage /> },
      { path: 'manual-review', element: <ManualReviewPage /> },
      { path: 'replies', element: <RepliesPage /> },
      { path: 'statistics', element: <StatisticsPage /> },
      { path: 'master-data', element: <AdminOnly><MasterDataPage /></AdminOnly> },
      { path: 'users', element: <AdminOnly><UsersPage /></AdminOnly> },
      { path: 'profile', element: <ProfilePage /> },
      { path: 'notification-center', element: <NotificationCenterPage /> },
      { path: 'notifications', element: <NotificationsPage /> },
      { path: 'ai-logs', element: <AiLogsPage /> },
      { path: 'system', element: <SystemPage /> },
    ],
  },
  {
    path: '*',
    element: <Navigate to="/" replace />,
  },
]);

export default router;
