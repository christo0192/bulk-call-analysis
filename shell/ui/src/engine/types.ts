export type JobState =
  | 'planned'
  | 'plan_approved'
  | 'running'
  | 'sample_ready'
  | 'sample_approved'
  | 'paused'
  | 'stalled'
  | 'done'
  | 'done_with_issues'
  | 'failed'
  | 'cancelled';
export type Project = {
  project: string;
  title: string;
  status?: JobState;
  activeJob?: JobSummary | null;
  lastJob?: JobSummary | null;
};
export type JobSummary = {
  job: string;
  project: string;
  title?: string;
  state: JobState;
  step?: string;
  createdAt?: number;
  updatedAt?: number;
  specVersion?: number;
  done?: number;
  total?: number;
  remaining?: number | string;
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
export type ChatEntry = {
  id: number;
  ts: number;
  turn: string;
  role: 'user' | 'assistant' | 'tool' | 'status';
  text?: string;
  name?: string;
  status?: 'started' | 'done' | 'failed';
  summary?: string;
};
export type JobSnapshot = JobStatus & { selected?: number; eta?: string };
export type PlanCard = {
  job: string;
  project: string;
  specVersion: number;
  title: string;
  question: string;
  leadSource: { kind: 'sql' | 'ids'; sql?: string; idCount?: number };
  callsScope: { mode: 'all' | 'window'; windowStart?: string; windowEnd?: string };
  counts: {
    selected: number;
    usable: number;
    noTranscript: number;
    transcriptionFailed: number;
    tooShort: number;
    notFound: number;
    capped: number;
    duplicates?: number;
    malformed?: number;
  };
  scanBytes: number;
  outputColumns: { name: string; type: string; description?: string }[];
  rules: { kind: string; name?: string; summary: string }[];
  sampleSize: number;
  estimate: {
    sampleSeconds: { low: number; high: number } | null;
    fullSeconds: { low: number; high: number } | null;
    codexPercent: { low: number; high: number } | null;
  };
  approved: boolean;
  approvedAt?: number;
  // Compatibility aliases for older consumers.
  version?: number;
  count?: number;
  usable?: number;
  noTranscript?: number;
  scanned?: string;
  columns?: string[];
};
export type SampleRow = {
  lead: string;
  answer: Record<string, unknown>;
  quotes: { field: string; text: string; ok: boolean; flag?: string }[];
  flags: string[];
  status: string;
};
export type SampleCard = {
  job: string;
  specVersion: number;
  total: number;
  read: number;
  quotesVerified: number;
  quotesRepaired: number;
  quotesFlagged: number;
  heldBack: number;
  failed: number;
  rows: SampleRow[];
  columns: string[];
  file: string | null;
  count?: number;
  passed?: number;
  flagged?: number;
};
export type DoneView = { total: number; completed: number; issues: string[] };
export type AllowanceWindow = { name: string; usedPercent: number; resetsAt: number };
export type AllowanceStatus = {
  codex: { windows: AllowanceWindow[]; stopAtPercent: number } | null;
  claude: { windows: AllowanceWindow[]; stopAtPercent?: number } | null;
};
export type Allowances = AllowanceStatus;
export type EngineStatus = {
  state: string;
  protocol: string;
  engineVersion: string;
  dataDir: string;
  runningJobs: string[] | number;
};
export type Settings = {
  connectorPath?: string;
  keyPath?: string;
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
  'setup/markManual': { params: { item: string }; result: { item: string; status: SetupStatus } };
  'settings/get': { params: {}; result: Settings };
  'settings/set': { params: Partial<Settings>; result: Settings };
  'projects/list': { params: {}; result: Project[] };
  'projects/create': { params: { project: string; title: string }; result: Project };
  'files/list': { params: { project: string }; result: FileInfo[] };
  'chat/send': {
    params: { project: string; text: string };
    result: { status: 'completed' | 'stopped'; project: string };
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
  'ids/attach': {
    params: { project: string; text: string };
    result: { name: string; raw: number; distinct: number; duplicates: number; malformed: number };
  };
  'job/outputs': { params: { job: string }; result: DoneView };
  'allowance/status': { params: {}; result: AllowanceStatus };
  'files/path': { params: { project: string; name: string }; result: { path: string } };
};
export type ParamsOf<M extends Method> = MethodMap[M]['params'];
export type ResultOf<M extends Method> = MethodMap[M]['result'];
