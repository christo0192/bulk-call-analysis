import type { JobSnapshot, Notification } from '../engine/types';

export type LiveInput =
  | { type: 'reset'; job: string }
  | { type: 'snapshot/requested'; sequence: number }
  | { type: 'snapshot/loaded'; sequence: number; snapshot: JobSnapshot }
  | { type: 'snapshot/failed'; sequence: number; error: string; code?: number | null }
  | { type: 'notification'; notification: Notification }
  | { type: 'tick'; state?: string };

export type JobLiveState = {
  job: string;
  snapshot: JobSnapshot | null;
  error: string | null;
  errorCode: number | null;
  requested: number;
  loaded: number;
  buffered: Notification[];
  stalled: boolean;
  reload: boolean;
};

export function initialJobLive(job: string): JobLiveState {
  return {
    job,
    snapshot: null,
    error: null,
    errorCode: null,
    requested: 0,
    loaded: 0,
    buffered: [],
    stalled: false,
    reload: false,
  };
}

function eventOf(notification: Notification): Record<string, unknown> {
  const params = notification.params;
  return (params.event as Record<string, unknown> | undefined) ?? params;
}

function applyNotification(state: JobLiveState, notification: Notification): JobLiveState {
  const params = notification.params;
  if (params.job !== state.job) return state;
  const snapshot = state.snapshot;
  if (!snapshot) return { ...state, buffered: [...state.buffered, notification] };
  if (state.requested > state.loaded)
    return { ...state, buffered: [...state.buffered, notification] };
  if (notification.method === 'job/progress') {
    const progress = { ...snapshot.progress };
    for (const key of [
      'done',
      'inFlight',
      'retrying',
      'failed',
      'heldBack',
      'remaining',
      'total',
    ]) {
      const value = params[key];
      if (typeof value === 'number') progress[key as keyof typeof progress] = value as never;
    }
    if (typeof params.phase === 'string') progress.phase = params.phase;
    return { ...state, snapshot: { ...snapshot, progress }, error: null, reload: false };
  }
  if (notification.method === 'job/state') {
    const nextState = typeof params.state === 'string' ? params.state : snapshot.state;
    const leavingPaused = snapshot.state === 'paused' && nextState !== 'paused';
    const leavingStalled = snapshot.state === 'stalled' && nextState !== 'stalled';
    return {
      ...state,
      snapshot: {
        ...snapshot,
        state: nextState,
        ...(typeof params.step === 'string' ? { step: params.step } : {}),
        ...(leavingPaused ? { pauseReason: null, resumeAt: null } : {}),
      },
      stalled: leavingStalled ? false : state.stalled,
      reload: true,
    };
  }
  const event = eventOf(notification);
  const kind = typeof event.kind === 'string' ? event.kind : '';
  const next: JobSnapshot = { ...snapshot };
  if (kind === 'eta') {
    next.eta = {
      low: typeof event.low === 'number' ? event.low : null,
      high: typeof event.high === 'number' ? event.high : null,
      seeded: event.seeded === true,
      known: event.known === true,
    };
  } else if (kind === 'pause') {
    next.pauseReason = typeof event.reason === 'string' ? event.reason : null;
    next.resumeAt = typeof event.resume_at === 'number' ? event.resume_at : null;
    next.state = 'paused';
  } else if (kind === 'activity') {
    next.activity = {
      ...next.activity,
      ...(typeof event.last_activity_ts === 'number' || event.last_activity_ts === null
        ? { lastActivityTs: event.last_activity_ts as number | null }
        : {}),
      ...(typeof event.in_flight === 'number' ? { inFlight: event.in_flight } : {}),
      ...(typeof event.workers === 'number' ? { workers: event.workers } : {}),
      ...(typeof event.throughput_per_min === 'number'
        ? { throughputPerMin: event.throughput_per_min }
        : {}),
    };
  } else if (kind === 'counts') {
    const progress = { ...next.progress };
    for (const key of [
      'done',
      'inFlight',
      'retrying',
      'failed',
      'heldBack',
      'remaining',
      'total',
    ]) {
      const value =
        event[key] ?? event[key.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`)];
      if (typeof value === 'number') progress[key as keyof typeof progress] = value as never;
    }
    if (typeof event.phase === 'string') progress.phase = event.phase;
    next.progress = progress;
  }
  return { ...state, snapshot: next, stalled: kind === 'stalled' ? true : state.stalled };
}

export function reduce(state: JobLiveState, input: LiveInput): JobLiveState {
  if (input.type === 'reset') {
    const next = initialJobLive(input.job);
    next.requested = state.requested + 1;
    next.loaded = next.requested;
    return next;
  }
  if (input.type === 'snapshot/requested')
    return input.sequence >= state.requested
      ? { ...state, requested: input.sequence, reload: false }
      : state;
  if (input.type === 'snapshot/loaded') {
    if (input.sequence < state.requested || input.sequence < state.loaded) return state;
    let next: JobLiveState = {
      ...state,
      snapshot: input.snapshot,
      loaded: input.sequence,
      error: null,
      errorCode: null,
      buffered: [],
      stalled: input.snapshot.state === 'stalled',
      reload: false,
    };
    for (const notification of state.buffered) next = applyNotification(next, notification);
    return { ...next, buffered: [] };
  }
  if (input.type === 'snapshot/failed') {
    if (input.sequence < state.requested) return state;
    return {
      ...state,
      error: state.snapshot ? "Couldn't refresh. Retry" : input.error,
      errorCode: input.code ?? null,
    };
  }
  if (input.type === 'notification') return applyNotification(state, input.notification);
  if (input.state && input.state !== 'stalled') return { ...state, stalled: false };
  return state;
}

export function dropJobLive(state: JobLiveState): JobLiveState {
  return { ...initialJobLive(state.job), requested: state.requested + 1 };
}
