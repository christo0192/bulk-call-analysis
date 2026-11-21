import type {
  Project,
  JobSummary,
  Allowances,
  FileInfo,
  SetupItem,
  AuthStatus,
  Settings,
  EngineStatus,
  PlanCard,
  SampleCard,
} from '../../engine/types';

export const projects: Project[] = [
  { project: 'demo-setup', title: 'Discount questions · Sep–Oct', status: 'planned' },
  { project: 'demo-sample', title: 'Certificate mentions · Jul–Oct', status: 'sample_ready' },
  { project: 'demo-run', title: 'Webinar objections · Oct', status: 'running' },
  { project: 'demo-paused', title: 'Renewal concerns · Nov', status: 'paused' },
  { project: 'demo-done', title: 'Shipping questions · Oct', status: 'done_with_issues' },
];
export const jobs: JobSummary[] = projects.map((p, i) => ({
  job: `synthetic-job-${i + 1}`,
  project: p.project,
  state: p.status!,
  done: p.status === 'running' ? 4312 : p.status === 'done_with_issues' ? 2104 : 0,
  total: p.status === 'done_with_issues' ? 2104 : 11058,
  remaining: p.status === 'running' ? 6746 : 0,
}));
export const allowances: Allowances = {
  codex: {
    windows: [
      { name: '5h', usedPercent: 4, resetsAt: Math.floor((Date.now() + 3600000) / 1000) },
      { name: 'weekly', usedPercent: 12, resetsAt: Math.floor((Date.now() + 86400000 * 3) / 1000) },
    ],
    stopAtPercent: 95,
  },
  claude: { checkedAt: Date.now(), rateLimitInfo: { status: 'ok' } },
};
export const plan: PlanCard = {
  job: 'synthetic-job-1',
  project: 'demo-setup',
  specVersion: 1,
  title: 'Discount questions',
  question: 'How often do callers ask for a discount, and what do they ask for?',
  leadSource: {
    kind: 'sql',
    sql: 'SELECT lead_id, transcript FROM calls WHERE created_at >= @start',
  },
  callsScope: { mode: 'window', windowStart: '2025-09-01', windowEnd: '2025-10-31' },
  counts: {
    selected: 11058,
    usable: 10727,
    noTranscript: 331,
    transcriptionFailed: 0,
    tooShort: 0,
    notFound: 0,
    capped: 57,
  },
  scanBytes: 184549376,
  outputColumns: [
    { name: 'asked_discount', type: 'boolean' },
    { name: 'discount_amount', type: 'string' },
    { name: 'reason', type: 'string' },
  ],
  rules: [
    { kind: 'answer', summary: 'Answer from the call, not metadata' },
    { kind: 'quote', summary: 'Include a short masked quote for every answer' },
  ],
  sampleSize: 20,
  estimate: null,
  approved: false,
};
export const planWithEstimate: PlanCard = {
  ...plan,
  estimate: {
    sampleSeconds: { low: 180, high: 300 },
    fullSeconds: { low: 25200, high: 32400 },
    codexPercent: { low: 35, high: 42 },
  },
};
export const sample: SampleCard = {
  job: 'synthetic-job-1',
  specVersion: 1,
  total: 20,
  read: 20,
  quotesVerified: 19,
  quotesRepaired: 1,
  quotesFlagged: 0,
  heldBack: 0,
  failed: 0,
  columns: ['asked_discount', 'discount_amount', 'reason'],
  file: 'sample.csv',
  rows: Array.from({ length: 5 }, (_, i) => ({
    lead: `lead-${i + 1}`,
    answer: {
      asked_discount: i % 2 === 0 ? 'Yes' : 'No',
      discount_amount: i % 2 === 0 ? '[PHONE] off' : '—',
      reason: i % 2 === 0 ? '[NAME] asked about price' : 'No request',
    },
    quotes: [{ field: 'reason', text: '[NAME] asked about the price', ok: true }],
    flags: [],
    status: 'ok',
  })),
};
export const files: FileInfo[] = [
  { name: 'brief.md', size: 1820 },
  { name: 'rules.json', size: 924 },
  { name: 'sample.csv', size: 48200 },
];
export const checklist: SetupItem[] = [
  { name: 'uv_python', status: 'ok' },
  { name: 'venv', status: 'ok' },
  { name: 'spacy', status: 'ok' },
  { name: 'connector', status: 'ok' },
  { name: 'key', status: 'ok' },
  { name: 'claude_copy', status: 'ok' },
  { name: 'codex_copy', status: 'ok' },
  { name: 'claude_login', status: 'ok' },
  { name: 'chatgpt_login', status: 'ok' },
  { name: 'training_off', status: 'manual_done' },
  { name: 'extra_usage_off', status: 'manual_done' },
];
export const newComputerChecklist: SetupItem[] = [
  ...checklist.slice(0, 7),
  { name: 'claude_login', status: 'pending' },
  { name: 'chatgpt_login', status: 'ok' },
  { name: 'training_off', status: 'manual_pending' },
  { name: 'extra_usage_off', status: 'manual_pending' },
];
export const authStatus: AuthStatus = {
  claude: { loggedIn: true },
  codex: { loggedIn: true, planType: 'Plus', usagePercent: 12 },
};
export const settings: Settings = {
  connectorPath: '',
  keyLocated: true,
  claudeModel: 'claude-sonnet',
  codexModel: 'gpt-5',
  workerCount: 0,
  retentionDays: 30,
};
export const appStatus: EngineStatus = {
  state: 'ready',
  protocol: 'json-rpc',
  engineVersion: '0.1.0',
  dataDir: 'Application data',
  runningJobs: [],
};
