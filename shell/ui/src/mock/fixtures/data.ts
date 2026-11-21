import type {
  Project,
  JobSummary,
  Allowances,
  FileInfo,
  SetupItem,
  AuthStatus,
  Settings,
  EngineStatus,
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
export const allowances: Allowances = { chatgpt: '12% used', claude: '9% used' };
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
