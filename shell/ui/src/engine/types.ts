export type JobState =
  | 'planned'
  | 'plan_approved'
  | 'running'
  | 'sample_ready'
  | 'paused'
  | 'stalled'
  | 'done'
  | 'done_with_issues'
  | 'failed'
  | 'cancelled';
export type Project = { project: string; title: string; status?: JobState };
export type JobSummary = {
  job: string;
  project: string;
  state: JobState;
  done?: number;
  total?: number;
  remaining?: number;
};
export type FileInfo = { name: string; size: number };
export type SetupStatus = 'pending' | 'ok' | 'failed' | 'manual_pending' | 'manual_done';
export type SetupItem = {
  name: string;
  status: SetupStatus;
  detail?: string;
  action_id?: string;
};
export type Counts = { done: number; total: number; remaining: number; failed?: number };
export type JobStatus = JobSummary & { counts?: Counts; reason?: string };
export type ChatEntry = { id: string; role: 'user' | 'assistant' | 'tool'; text: string };
export type JobSnapshot = JobStatus & { selected?: number; eta?: string };
export type PlanCard = {
  version: number;
  count: number;
  usable: number;
  noTranscript: number;
  scanned: string;
  columns: string[];
};
export type SampleCard = { count: number; passed: number; flagged: number };
export type DoneView = { total: number; completed: number; issues: string[] };
export type Allowances = { chatgpt?: string | number; claude?: string | number };
export type EngineStatus = {
  state: string;
  protocol: string;
  engineVersion: string;
  dataDir: string;
  runningJobs: number;
};
export type Settings = {
  connectorPath: string;
  keyLocated: boolean;
  claudeModel: string;
  codexModel: string;
  workerCount: number;
  retentionDays: number;
  confirmModelChange?: boolean;
};
export type AuthAccount = { loggedIn: boolean; planType?: string; usagePercent?: number };
export type AuthStatus = { claude: AuthAccount; codex: AuthAccount };
export type Notification = {
  jsonrpc: '2.0';
  method: 'job/state' | 'job/progress' | 'chat/event' | 'engine/event';
  params: Record<string, unknown>;
};
export type Params = { [key: string]: unknown };
export type Method = keyof MethodMap;
export type MethodMap = {
  'app/status': { params: {}; result: EngineStatus };
  'health/ping': { params: {}; result: { status: 'ok' } };
  shutdown: { params: {}; result: { stopping: true; exitCode: 0 } };
  'setup/checklist': { params: {}; result: SetupItem[] };
  'setup/runChecks': { params: {}; result: { items: SetupItem[] } };
  'setup/markManual': { params: { item: string }; result: SetupItem };
  'settings/get': { params: {}; result: Settings };
  'settings/set': { params: Partial<Settings>; result: Settings };
  'projects/list': { params: {}; result: Project[] };
  'projects/create': { params: { project: string; title: string }; result: Project };
  'files/list': { params: { project: string }; result: FileInfo[] };
  'chat/send': {
    params: { project: string; text: string };
    result: { status: string; project: string };
  };
  'chat/stop': { params: { project: string }; result: { status: 'stopped'; project: string } };
  'job/status': { params: { job: string }; result: JobStatus };
  'job/approvePlan': { params: { job: string; expectedSpecVersion: number }; result: JobStatus };
  'job/approveSample': { params: { job: string; expectedSpecVersion: number }; result: JobStatus };
  'job/startFull': { params: { job: string }; result: JobStatus };
  'job/pause': { params: { job: string }; result: JobStatus };
  'job/resume': { params: { job: string }; result: JobStatus };
  'job/rebuildRules': {
    params: { job: string; rules: unknown[] };
    result: { job: string; path: string };
  };
  'auth/claudeLogin': { params: {}; result: { started: true; vendor: 'claude' } };
  'auth/codexLogin': { params: {}; result: { started: true; vendor: 'codex' } };
  'auth/status': { params: {}; result: AuthStatus };
  'os/suspendHint': { params: {}; result: { status: 'accepted' } };
  'os/resumeHint': { params: {}; result: { status: 'accepted'; actions: string[] } };
  'os/powerStatus': {
    params: { onAc: boolean | null; percent: number | null };
    result: { status: 'accepted' };
  };
  // planned (M002-S01)
  'projects/jobs': { params: { project: string }; result: { jobs: JobSummary[] } };
  'chat/history': {
    params: { project: string; before?: string; limit?: number };
    result: { entries: ChatEntry[]; more: boolean };
  };
  'job/snapshot': { params: { job: string }; result: JobSnapshot };
  'job/plan': { params: { job: string }; result: PlanCard };
  'job/sample': { params: { job: string }; result: SampleCard };
  'job/outputs': { params: { job: string }; result: DoneView };
  'allowance/status': { params: {}; result: Allowances };
  'files/path': { params: { project: string; name: string }; result: { path: string } };
};
export type ParamsOf<M extends Method> = MethodMap[M]['params'];
export type ResultOf<M extends Method> = MethodMap[M]['result'];
