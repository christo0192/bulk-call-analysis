import { engineStatus, load, loadError } from '../state/store';
export function EngineStatusBanner() {
  const status = engineStatus.value;
  if (status === 'ready' && !loadError.value) return null;
  if (status === 'ready' && loadError.value)
    return (
      <div class="banner" role="status">
        {loadError.value} <button onClick={() => void load()}>Try again</button>
      </div>
    );
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
