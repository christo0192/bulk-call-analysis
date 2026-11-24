import type { PlanCard } from './types';

const numberOr = (value: unknown, fallback = 0): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : fallback;

export function adaptPlan(value: unknown): PlanCard {
  const raw = (value ?? {}) as Record<string, unknown>;
  const counts = (raw.counts ?? {}) as Record<string, unknown>;
  const leadSource = (raw.leadSource ?? {}) as Record<string, unknown>;
  const callsScope = (raw.callsScope ?? {}) as Record<string, unknown>;
  const estimateValue = raw.estimate;
  const estimate =
    estimateValue && typeof estimateValue === 'object'
      ? (estimateValue as Record<string, unknown>)
      : null;
  const range = (value: unknown) => {
    if (!value || typeof value !== 'object') return null;
    const item = value as Record<string, unknown>;
    return { low: numberOr(item.low), high: numberOr(item.high) };
  };
  return {
    job: typeof raw.job === 'string' ? raw.job : '',
    project: typeof raw.project === 'string' ? raw.project : '',
    specVersion: numberOr(raw.specVersion ?? raw.version),
    title: typeof raw.title === 'string' ? raw.title : '',
    question: typeof raw.question === 'string' ? raw.question : '',
    leadSource: {
      kind: leadSource.kind === 'sql' ? 'sql' : 'ids',
      sql: typeof leadSource.sql === 'string' ? leadSource.sql : undefined,
      idCount: numberOr(leadSource.idCount),
    },
    callsScope: {
      mode: callsScope.mode === 'all' ? 'all' : 'window',
      windowStart: typeof callsScope.windowStart === 'string' ? callsScope.windowStart : undefined,
      windowEnd: typeof callsScope.windowEnd === 'string' ? callsScope.windowEnd : undefined,
    },
    counts: {
      selected: numberOr(counts.selected),
      usable: numberOr(counts.usable),
      noTranscript: numberOr(counts.noTranscript ?? counts.no_transcript),
      transcriptionFailed: numberOr(counts.transcriptionFailed ?? counts.transcription_failed),
      tooShort: numberOr(counts.tooShort ?? counts.too_short),
      notFound: numberOr(counts.notFound ?? counts.not_found),
      capped: numberOr(counts.capped),
      duplicates: numberOr(counts.duplicates),
      malformed: numberOr(counts.malformed),
    },
    scanBytes: numberOr(raw.scanBytes ?? raw.scanned),
    outputColumns: Array.isArray(raw.outputColumns)
      ? raw.outputColumns.filter(
          (item): item is { name: string; type: string; description?: string } =>
            typeof item === 'object' &&
            item !== null &&
            typeof (item as { name?: unknown }).name === 'string',
        )
      : [],
    rules: Array.isArray(raw.rules)
      ? raw.rules.filter(
          (item): item is { kind: string; name?: string; summary: string } =>
            typeof item === 'object' &&
            item !== null &&
            typeof (item as { summary?: unknown }).summary === 'string',
        )
      : [],
    sampleSize: numberOr(raw.sampleSize),
    estimate: estimate
      ? {
          sampleSeconds: range(estimate.sampleSeconds),
          fullSeconds: range(estimate.fullSeconds),
          codexPercent: range(estimate.codexPercent),
        }
      : null,
    approved: raw.approved === true,
    approvedAt: typeof raw.approvedAt === 'number' ? raw.approvedAt : undefined,
  };
}
