export function SetupView({ id }: { id: string }) {
  return (
    <div class="placeholder" data-testid="setup-view">
      <h2>Setup</h2>
      <p>Job {id}</p>
    </div>
  );
}
