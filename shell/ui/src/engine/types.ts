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
  approvedAt?: number;
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
  toolId?: string;
  status?: 'started' | 'done' | 'failed';
  summary?: string;
};
export type SnapshotProgress = {
  job: string;
  project: string;
  phase: string;
  done: number;
  inFlight: number;
  retrying: number;
  failed: number;
  heldBack: number;
  remaining: number;
  total: number;
};
export type SnapshotCounts = {
  selected: number;
  usable: number;
  noTranscript: number;
  transcriptionFailed: number;
  tooShort: number;
  notFound: number;
  capped: number;
  failedByReason: Record<string, number>;
};
export type SnapshotEta = {
  low: number | null;
  high: number | null;
  seeded: boolean;
  known: boolean;
};
export type JobSnapshot = {
  job: string;
  project: string;
  title: string;
  state: JobState | string;
  step: string;
  specVersion: number;
  pauseReason: string | null;
  resumeAt: number | null;
  planApprovedAt: number | null;
  sampleApprovedAt: number | null;
  progress: SnapshotProgress;
  counts: SnapshotCounts;
  eta: SnapshotEta | null;
  activity: {
    lastActivityTs: number | null;
    inFlight: number;
    workers: number;
    throughputPerMin: number | null;
  };
  quota: {
    codex: {
      primary: { usedPercent: number; windowDurationMins: number; resetsAt: number } | null;
      stopAtPercent?: number;
      projectedEndPercent?: number | null;
      [key: string]: unknown;
    } | null;
    claude: Record<string, unknown> | null;
    projectedEndPercent: number | null;
  };
  steps: {
    id: string;
    label: string;
    status: 'done' | 'active' | 'todo' | string;
    ts?: number;
    detail: string | null;
  }[];
  recent: { ts: number; kind: string; text: string }[];
  reconciliation: { addsUp: boolean; [key: string]: unknown } | null;
  approvedAt?: number;
};
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
  } | null;
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
export type OutputFile = { name: string; kind: string };
export type Reconciliation = {
  selected: number;
  read: number;
  noTranscript: number;
  transcriptionFailed: number;
  tooShort: number;
  notFound: number;
  heldBack: number;
  failed: number;
  notStarted: number;
  addsUp: boolean;
};
export type OutputIssue = { kind: string; count: number; text: string; leads: string[] };
export type JobOutputs = {
  job: string;
  specVersion: number;
  state: string;
  finishedAt: number;
  durationSeconds: number | null;
  asleepSeconds: number | null;
  files: OutputFile[];
  reconciliation: Reconciliation;
  issues: OutputIssue[];
  headline: string | null;
};
export type RuleCondition = {
  field: string;
  op: string;
  value?: string | number | boolean | null | (string | number | boolean)[];
};
export type Rule =
  | { kind: 'in_scope'; match: 'all' | 'any'; conditions: RuleCondition[] }
  | { kind: 'threshold_group'; name: string; field: string; edges: number[]; labels: string[] }
  | { kind: 'review_flag'; name: string; match: 'all' | 'any'; conditions: RuleCondition[] };
export type RebuildResult = { job: string; path: string; rebuild: number };
export type AllowanceWindow = { name: string; usedPercent: number; resetsAt: number };
export type AllowanceStatus = {
  codex: { windows: AllowanceWindow[]; stopAtPercent: number } | null;
  claude: {
    windows?: AllowanceWindow[];
    stopAtPercent?: number;
    checkedAt?: number;
    rateLimitInfo?: unknown;
  } | null;
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
    params: { job: string; rules: Rule[] };
    result: RebuildResult;
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
    params: { project: string; before?: number; limit?: number };
    result: { entries: ChatEntry[]; more: boolean };
  };
  'job/snapshot': { params: { job: string }; result: JobSnapshot };
  'job/plan': { params: { job: string }; result: PlanCard };
  'job/sample': { params: { job: string }; result: SampleCard };
  'ids/attach': {
    params: { project: string; text: string };
    result: { name: string; raw: number; distinct: number; duplicates: number; malformed: number };
  };
  'job/outputs': { params: { job: string }; result: JobOutputs };
  'allowance/status': { params: {}; result: AllowanceStatus };
  open_project_folder: { params: { project: string }; result: null };
  'files/path': { params: { project: string; name: string }; result: { path: string } };
};
export type ParamsOf<M extends Method> = MethodMap[M]['params'];
export type ResultOf<M extends Method> = MethodMap[M]['result'];
