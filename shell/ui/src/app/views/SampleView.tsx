export function SampleView({ id }: { id: string }) {
  return (
    <div class="placeholder" data-testid="sample-view">
      <h2>Sample</h2>
      <p>Job {id}</p>
    </div>
  );
}
