export function RunView({ id }: { id: string }) {
  return (
    <div class="placeholder" data-testid="run-view">
      <h2>Run</h2>
      <p>Job {id}</p>
    </div>
  );
}
