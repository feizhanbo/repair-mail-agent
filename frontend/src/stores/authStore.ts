import { create } from 'zustand';
import type { CurrentUser } from '../types/api';

type AuthState = {
  token: string | null;
  user: CurrentUser | null;
  setSession: (token: string, user: CurrentUser) => void;
  clearSession: () => void;
};

const storedUser = localStorage.getItem('repair_mail_user');
localStorage.removeItem('repair_mail_token');

export const useAuthStore = create<AuthState>((set) => ({
  token: storedUser ? 'http-only-cookie' : null,
  user: storedUser ? (JSON.parse(storedUser) as CurrentUser) : null,
  setSession: (_token, user) => {
    localStorage.setItem('repair_mail_user', JSON.stringify(user));
    set({ token: 'http-only-cookie', user });
  },
  clearSession: () => {
    localStorage.removeItem('repair_mail_user');
    set({ token: null, user: null });
  },
}));
