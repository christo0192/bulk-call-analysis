import { engineStatus } from '../state/store';
export function EngineStatusBanner() {
  const status = engineStatus.value;
  if (status === 'ready') return null;
  const text =
    status === 'starting'
      ? 'Starting the engine…'
      : status === 'restarting'
        ? 'The engine stopped unexpectedly. Restarting…'
        : status === 'stopped'
          ? 'The engine is stopped.'
          : 'The engine is unavailable.';
  return (
    <div class="banner" role="status">
      {text}
    </div>
  );
}
