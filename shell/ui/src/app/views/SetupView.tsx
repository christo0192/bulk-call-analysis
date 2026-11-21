export function SetupView({ id }: { id?: string }) {
  return (
    <div class="placeholder" data-testid="setup-view">
      <h2>Setup</h2>
      {id && <p>Job {id}</p>}
    </div>
  );
}
