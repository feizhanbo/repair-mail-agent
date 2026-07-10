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

export type JsonRecord = Record<string, unknown>;

export type DatabaseColumnInfo = {
  name: string;
  type: string;
  nullable: boolean;
  default?: string | null;
  comment: string;
};

export type DatabaseTableInfo = {
  table_name: string;
  table_comment: string;
  columns: DatabaseColumnInfo[];
};

export type DatabaseTablesResponse = {
  tables: DatabaseTableInfo[];
  total: number;
};

export type DatabaseRowsResponse = {
  rows: JsonRecord[];
  total: number;
  page: number;
  page_size: number;
  columns: string[];
};

export type RoleCode = 'admin' | 'supervisor' | 'operator';

export type LoginRequest = {
  username: string;
  password: string;
};

export type CurrentUser = {
  id: number;
  username: string;
  real_name: string;
  email?: string | null;
  phone?: string | null;
  status?: string;
  roles: RoleCode[];
};

export type LoginResponse = {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: CurrentUser;
};

export type JobRunLog = {
  id: number;
  job_name: string;
  job_type: string;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  processed_count?: number;
  success_count?: number;
  failed_count?: number;
  error_message?: string | null;
};

export type DashboardSummary = {
  new_emails: number;
  pending_parse: number;
  manual_review: number;
  manual_review_tasks: number;
  task_pool_total?: number;
  need_manual_processing?: number;
  current_user_pending_tasks?: number;
  in_progress_tasks?: number;
  all_in_progress_tasks?: number;
  completed_exportable?: number;
  resolved_manual_tasks?: number;
  need_customer_info: number;
  auto_replied: number;
  error: number;
  ready_for_export: number;
  ai_low_confidence: number;
  recent_failed_jobs: JobRunLog[];
};

export type EmailItem = {
  id: number;
  thread_id?: number | null;
  mail_direction: string;
  mailbox_account: string;
  folder_name?: string | null;
  imap_uid?: string | null;
  message_id?: string | null;
  in_reply_to?: string | null;
  from_address: string;
  from_domain?: string | null;
  to_addresses?: string | null;
  cc_addresses?: string | null;
  subject?: string | null;
  normalized_subject?: string | null;
  sent_at?: string | null;
  received_at?: string | null;
  parse_status: string;
  intent_type?: string | null;
  duplicate_of_email_id?: number | null;
  error_message?: string | null;
  clean_body?: string | null;
  latest_reply_segment?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type Attachment = {
  id: number;
  email_id: number;
  oss_object_id?: number | null;
  file_name: string;
  content_type?: string | null;
  file_size?: number | null;
  file_hash?: string | null;
  is_inline?: boolean | null;
  content_id?: string | null;
  parse_status: string;
  extracted_text?: string | null;
  extracted_json?: JsonRecord | null;
  parse_error?: string | null;
  created_at?: string;
};

export type ParseResult = {
  id: number;
  email_id: number;
  source_attachment_id?: number | null;
  ticket_id?: number | null;
  parser_type: string;
  parser_version?: string | null;
  intent_type?: string | null;
  extracted_fields?: JsonRecord | null;
  extracted_items?: JsonRecord | null;
  missing_fields?: JsonRecord | null;
  conflict_fields?: JsonRecord | null;
  confidence_score?: number | string | null;
  field_confidences?: JsonRecord | null;
  evidence?: JsonRecord | null;
  apply_status: string;
  applied_by_user_id?: number | null;
  applied_at?: string | null;
  accepted: boolean;
  accepted_by_user_id?: number | null;
  accepted_at?: string | null;
  error_message?: string | null;
  created_at?: string;
};

export type EmailDetail = {
  email: EmailItem;
  attachments: Attachment[];
  parse_results: ParseResult[];
};

export type EmailIngestResult = {
  duplicate: boolean;
  email: EmailItem;
  classification?: {
    confidence?: number | string | null;
    reason?: string | null;
  };
  parse?: JsonRecord | null;
};

export type EmailTicketLink = {
  id: number;
  email_id: number;
  ticket_id: number;
  link_type: string;
  link_reason?: string | null;
  linked_by_user_id?: number | null;
  created_at?: string;
};

export type OperationLog = {
  id: number;
  user_id?: number | null;
  operation_type: string;
  target_type: string;
  target_id?: number | null;
  description?: string | null;
  before_data?: JsonRecord | null;
  after_data?: JsonRecord | null;
  created_at?: string;
};

export type EmailFlowTrace = {
  runtime_config: Pick<SystemConfig, 'auto_send_enabled' | 'reply_send_mode' | 'auto_send_min_confidence' | 'confidence_threshold' | 'max_follow_up'>;
  ingest_result?: JsonRecord | null;
  trace_only: boolean;
  email: EmailItem;
  email_id: number;
  attachments: Attachment[];
  email_ticket_links: EmailTicketLink[];
  parse_results: ParseResult[];
  tickets: Ticket[];
  ticket_items: TicketLine[];
  manual_review_tasks: ManualTask[];
  reply_records: ReplyRecord[];
  ai_call_logs: AiLog[];
  notification_events: NotificationEvent[];
  ticket_status_logs: StatusLog[];
  field_audit_logs: FieldAuditLog[];
  sn_validation_results: SnValidationResult[];
  operation_logs: OperationLog[];
};

export type EmailIngestRequest = {
  mailbox_account?: string;
  folder_name?: string;
  imap_uid?: string;
  message_id?: string;
  in_reply_to?: string;
  references_header?: string;
  from_address: string;
  to_addresses?: string;
  cc_addresses?: string;
  subject?: string;
  text_body?: string;
  html_body?: string;
};

export type Ticket = {
  id: number;
  ticket_no: string;
  current_status_code: string;
  source_email_id?: number | null;
  thread_id?: number | null;
  customer_code?: string | null;
  customer_name?: string | null;
  contact_person?: string | null;
  contact_phone?: string | null;
  contact_email?: string | null;
  request_date?: string | null;
  mailing_address?: string | null;
  problem_description?: string | null;
  accessories?: string | null;
  missing_fields?: JsonRecord | null;
  conflict_fields?: JsonRecord | null;
  followup_count: number;
  max_followup_count: number;
  confidence_score?: number | string | null;
  assigned_user_id?: number | null;
  manual_locked: boolean;
  version: number;
  created_at?: string;
  updated_at?: string;
};

export type TicketLine = {
  id: number;
  ticket_id: number;
  line_no: number;
  material_code?: string | null;
  material_name?: string | null;
  sn?: string | null;
  sn_asset_id?: number | null;
  quantity: number;
  failure_description?: string | null;
  failure_information?: string | null;
  data_info?: string | null;
  remarks?: string | null;
  accessories?: string | null;
  validation_status: string;
  validation_message?: string | null;
  manual_locked: boolean;
  created_at?: string;
  updated_at?: string;
};

export type StatusLog = {
  id: number;
  ticket_id?: number;
  from_status_code?: string | null;
  to_status_code: string;
  trigger_event: string;
  reason?: string | null;
  operator_type: string;
  operator_user_id?: number | null;
  metadata_json?: JsonRecord | null;
  created_at?: string;
};

export type EmailThreadContext = {
  id: number;
  thread_key?: string | null;
  normalized_subject?: string | null;
  root_message_id?: string | null;
  latest_email_id?: number | null;
  ticket_id?: number | null;
  email_count?: number | null;
  merge_confidence?: number | string | null;
  merge_reason?: string | null;
  manual_locked?: boolean | null;
  created_at?: string;
  updated_at?: string;
};

export type SnValidationResult = {
  id: number;
  ticket_id: number;
  ticket_item_id?: number | null;
  sn?: string | null;
  matched_sn_asset_id?: number | null;
  check_exists?: boolean | null;
  check_valid?: boolean | null;
  check_customer_match?: boolean | null;
  check_material_match?: boolean | null;
  need_ship_to_beijing?: boolean | null;
  result_status: string;
  result_message?: string | null;
  checked_by?: string | null;
  checked_at?: string | null;
};

export type FieldAuditLog = {
  id: number;
  ticket_id: number;
  ticket_item_id?: number | null;
  field_name: string;
  old_value?: string | null;
  new_value?: string | null;
  source_type: string;
  reason?: string | null;
  operator_user_id?: number | null;
  parse_result_id?: number | null;
  created_at?: string;
};

export type FieldEvidence = {
  parse_evidence: ParseResult[];
  field_audits: FieldAuditLog[];
};

export type TicketDetail = {
  ticket: Ticket;
  items: TicketLine[];
  source_email?: EmailItem | null;
  thread?: EmailThreadContext | null;
  parse_results: ParseResult[];
  sn_validation_results: SnValidationResult[];
  manual_tasks: ManualTask[];
  reply_records: ReplyRecord[];
  status_logs: StatusLog[];
  email_timeline: EmailItem[];
  attachments: Attachment[];
  field_evidence: FieldEvidence;
};

export type ManualTask = {
  id: number;
  ticket_id: number;
  email_id?: number | null;
  task_type: string;
  priority: string;
  status: string;
  description?: string | null;
  trigger_reason?: string | null;
  assigned_user_id?: number | null;
  claimed_by_user_id?: number | null;
  claimed_at?: string | null;
  resolved_by_user_id?: number | null;
  resolved_at?: string | null;
  resolution?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type UserAccount = {
  id: number;
  username: string;
  real_name: string;
  email?: string | null;
  phone?: string | null;
  status: string;
  roles: RoleCode[];
  last_login_at?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type UserCreateRequest = {
  username: string;
  password: string;
  real_name: string;
  email?: string | null;
  phone?: string | null;
  status: 'active' | 'disabled';
  roles: RoleCode[];
};

export type UserUpdateRequest = {
  real_name?: string;
  email?: string | null;
  phone?: string | null;
};

export type ManualTaskDetail = {
  task: ManualTask;
  ticket_context: TicketDetail;
};

export type ManualTaskReparseResponse = {
  task: ManualTask;
  ticket_context: TicketDetail;
  reparse_result: JsonRecord;
};

export type ReplyRecord = {
  id: number;
  ticket_id: number;
  related_email_id?: number | null;
  outgoing_email_id?: number | null;
  template_id?: number | null;
  reply_type: string;
  followup_round: number;
  missing_fields?: JsonRecord | null;
  to_addresses: string;
  cc_addresses?: string | null;
  subject?: string | null;
  draft_body?: string | null;
  final_body?: string | null;
  generate_source: string;
  review_status: string;
  reviewed_by_user_id?: number | null;
  reviewed_at?: string | null;
  send_status: string;
  smtp_message_id?: string | null;
  sent_at?: string | null;
  error_message?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type SnAsset = {
  id: number;
  customer_code: string;
  customer_name: string;
  material_code: string;
  material_name?: string | null;
  sn: string;
  asset_status: string;
  imported_at?: string | null;
};

export type BoardCard = {
  id: number;
  material_code: string;
  material_name?: string | null;
  need_ship_to_beijing: boolean;
  shipping_address?: string | null;
  status: string;
};

export type AiLog = {
  id: number;
  trace_id: string;
  email_id?: number | null;
  ticket_id?: number | null;
  call_type: string;
  provider_name?: string | null;
  model_name: string;
  prompt_version: string;
  input_summary?: string | null;
  output_summary?: string | null;
  parsed_key_result?: JsonRecord | null;
  confidence_score?: number | string | null;
  latency_ms?: number | null;
  status: string;
  error_message?: string | null;
  log_file_path?: string | null;
  log_line_no?: number | null;
  log_record_hash?: string | null;
  created_at?: string;
};

export type NotificationEvent = {
  id: number;
  event_type: string;
  target_type: string;
  target_id: number;
  title: string;
  content?: string | null;
  priority: string;
  recipient_user_id?: number | null;
  recipient_role_code?: string | null;
  delivery_channel?: string;
  delivery_status: string;
  read_at?: string | null;
  metadata?: JsonRecord | null;
  metadata_json?: JsonRecord | null;
  delivered_at?: string | null;
  created_at?: string;
};

export type WorkflowStatus = {
  id: number;
  status_code: string;
  status_name: string;
  status_category: string;
  description?: string | null;
  is_terminal: boolean;
  sort_order: number;
  enabled: boolean;
};

export type WorkflowTransition = {
  id: number;
  from_status_code: string;
  to_status_code: string;
  trigger_event: string;
  condition_desc?: string | null;
  require_manual: boolean;
  enabled: boolean;
};

export type SystemInfo = {
  app: string;
  env: string;
  auto_send_enabled: boolean;
  reply_send_mode: 'human_review' | 'auto_send';
  auto_send_min_confidence: number;
  max_follow_up: number;
  confidence_threshold: number;
  environment_note?: string;
  integrations: JsonRecord;
  workflow_statuses: WorkflowStatus[];
  workflow_transitions: WorkflowTransition[];
};

export type SystemConfig = {
  auto_send_enabled: boolean;
  reply_send_mode: 'human_review' | 'auto_send';
  auto_send_min_confidence: number;
  max_follow_up: number;
  confidence_threshold: number;
  environment_note?: string;
  integrations: JsonRecord;
};

export type ReplyTemplate = {
  id: number;
  template_code: string;
  template_name: string;
  template_type: string;
  language: string;
  version: string;
  subject_template?: string | null;
  body_template: string;
  enabled: boolean;
  created_by_user_id?: number | null;
  created_at?: string;
  updated_at?: string;
};

export type StatisticsTrendItem = {
  label: string;
  start_date: string;
  end_date: string;
  email_count: number;
  ticket_count: number;
  completed_count: number;
  reparse_count: number;
};

export type UserProcessingStat = {
  user_id: number;
  real_name: string;
  username: string;
  resolved_count: number;
};

export type StatisticsSummary = {
  period: 'week' | 'month' | 'year';
  start_date: string;
  end_date: string;
  email_count: number;
  ticket_count: number;
  completed_count: number;
  reparse_count: number;
  ai_success_rate: number;
  auto_reply_rate: number;
  manual_intervention_rate: number;
  task_pool_total: number;
  need_customer_info: number;
  error_ticket_count: number;
  ready_for_export: number;
  status_distribution: { status_code: string; count: number }[];
  user_processing: UserProcessingStat[];
  trend: StatisticsTrendItem[];
};
