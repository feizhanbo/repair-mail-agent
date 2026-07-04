import { createBrowserRouter, Navigate } from 'react-router-dom';
import AppLayout from '../layouts/AppLayout';
import AiLogsPage from '../pages/AiLogsPage';
import Dashboard from '../pages/Dashboard';
import EmailsPage from '../pages/EmailsPage';
import Login from '../pages/Login';
import ManualReviewPage from '../pages/ManualReviewPage';
import MasterDataPage from '../pages/MasterDataPage';
import RepliesPage from '../pages/RepliesPage';
import SystemPage from '../pages/SystemPage';
import TicketsPage from '../pages/TicketsPage';

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
      { path: 'master-data', element: <MasterDataPage /> },
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
