import { newProjectOpen } from '../state/store';
export function EmptyState() {
  return (
    <div class="empty">
      <h2>No projects yet</h2>
      <p>Create a project to get started.</p>
      <button
        class="primary"
        onClick={() => {
          newProjectOpen.value = true;
        }}
      >
        New project
      </button>
    </div>
  );
}
