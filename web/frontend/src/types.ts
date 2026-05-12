/**
 * Mirror types from kbagent serve. These are intentionally permissive (`Record`
 * for nested blobs) so service-layer changes don't break the UI build. The
 * payloads are validated in pages where it matters via zod or runtime checks.
 */

export interface Project {
  alias: string;
  project_name: string;
  project_id: number | null;
  stack_url: string;
  token: string; // already masked
  is_default: boolean;
  active_branch_id: number | null;
}

export interface ProjectStatus {
  alias: string;
  stack_url: string;
  status: "ok" | "error";
  response_time_ms: number;
  project_name?: string;
  error?: string;
  error_code?: string;
}

export interface Bucket {
  project_alias: string;
  id: string;
  display_name: string;
  stage: string;
  backend: string;
  rows_count: number;
  data_size_bytes: number;
  description: string;
  is_linked: boolean;
  source_project_id: number | null;
  source_project_name: string;
  source_bucket_id: string;
}

export interface Table {
  project_alias: string;
  id: string;
  name: string;
  display_name: string;
  bucket_id: string;
  rows_count: number;
  data_size_bytes: number;
  is_alias: boolean;
  last_import_date: string;
}

export interface ConfigSummary {
  project_alias: string;
  component_id: string;
  component_name: string;
  component_type: string;
  config_id: string;
  config_name: string;
  config_description?: string;
  folder?: string;
  last_modified?: string;
  last_modified_by?: string;
}

export interface Job {
  project_alias: string;
  id: string | number;
  status: string;
  component: string;
  configId: string;
  createdTime: string;
  startTime?: string;
  endTime?: string;
  durationSeconds?: number;
  url?: string;
}

export interface Branch {
  project_alias: string;
  id: number;
  name: string;
  isDefault: boolean;
  description: string;
  created: string;
}

export interface Workspace {
  project_alias: string;
  id: number;
  name: string;
  backend: string;
  host: string;
  schema: string;
  user: string;
  created: string;
  component_id: string;
  config_id: string;
}

export interface Flow {
  project_alias: string;
  component_id: string;
  config_id: string;
  name: string;
  description: string;
  is_disabled: boolean;
  schedules?: Array<{ schedule_id: string; cron_tab: string; state: string }>;
}

export interface Schedule {
  project_alias: string;
  schedule_id: string;
  cron_tab: string;
  timezone: string;
  state: string;
  target?: { component_id: string; config_id: string; name?: string };
}

export interface DataApp {
  project_alias: string;
  app_id: string;
  config_id: string;
  name: string;
  type: string;
  state: string;
  desired_state: string;
  url: string;
  size: string;
}

export interface SharedBucket {
  source_project_id: number;
  source_bucket_id: string;
  source_bucket_name: string;
  sharing_type: string;
}

export interface LineageEdge {
  source_project_id: number;
  source_project_alias: string;
  source_project_name: string;
  source_bucket_id: string;
  sharing_type: string;
  target_project_id: number;
  target_project_alias: string;
  target_project_name: string;
  target_bucket_id: string;
}

export interface McpTool {
  name: string;
  description: string;
  multi_project: boolean;
  inputSchema: Record<string, unknown>;
}

export interface DoctorCheck {
  name: string;
  status: "pass" | "fail" | "warn" | "skip";
  message: string;
  details?: Record<string, unknown>;
}

export interface ProjectError {
  project_alias: string;
  error_code: string;
  message: string;
}

export interface Component {
  component_id: string;
  component_name: string;
  component_type: string;
  description?: string;
}
