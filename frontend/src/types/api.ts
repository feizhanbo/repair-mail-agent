export type ApiResponse<T> = {
  success: boolean;
  data: T;
  message: string;
  request_id: string;
};

export type PageData<T> = {
  items: T[];
  total: number;
  page: number;
  page_size: number;
};

