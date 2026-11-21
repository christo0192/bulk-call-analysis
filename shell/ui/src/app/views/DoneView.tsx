export function DoneView({ id }: { id: string }) {
  return (
    <div class="placeholder" data-testid="done-view">
      <h2>Done</h2>
      <p>Job {id}</p>
    </div>
  );
}
