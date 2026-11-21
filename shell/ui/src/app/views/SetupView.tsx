import { Chat } from '../chat/Chat';
import { PlanCard } from '../cards/Cards';
import { selectedProject } from '../../state/store';

export function SetupView({ id, state }: { id?: string; state?: string }) {
  const project = selectedProject.value ?? '';
  return (
    <div class="phase-view" data-testid="setup-view">
      <Chat project={project} />
      {id && state === 'planned' && <PlanCard job={id} />}
      <span class="sr-only">Job {id}</span>
    </div>
  );
}
