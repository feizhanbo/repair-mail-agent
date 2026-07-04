import { create } from 'zustand';
import type { CurrentUser } from '../types/api';

type AuthState = {
  token: string | null;
  user: CurrentUser | null;
  setSession: (token: string, user: CurrentUser) => void;
  clearSession: () => void;
};

const storedToken = localStorage.getItem('repair_mail_token');
const storedUser = localStorage.getItem('repair_mail_user');

export const useAuthStore = create<AuthState>((set) => ({
  token: storedToken,
  user: storedUser ? (JSON.parse(storedUser) as CurrentUser) : null,
  setSession: (token, user) => {
    localStorage.setItem('repair_mail_token', token);
    localStorage.setItem('repair_mail_user', JSON.stringify(user));
    set({ token, user });
  },
  clearSession: () => {
    localStorage.removeItem('repair_mail_token');
    localStorage.removeItem('repair_mail_user');
    set({ token: null, user: null });
  },
}));
