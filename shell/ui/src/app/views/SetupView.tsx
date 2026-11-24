import { useRef } from 'preact/hooks';
import { Chat } from '../chat/Chat';
import { PlanCard } from '../cards/Cards';
import { selectedProject } from '../../state/store';

export function SetupView({ id, state }: { id?: string; state?: string }) {
  const project = selectedProject.value ?? '';
  const composer = useRef<((prefix?: string) => void) | null>(null);
  return (
    <div class="phase-view" data-testid="setup-view">
      <Chat key={project} project={project} composer={composer}>
        {id && state === 'planned' && (
          <PlanCard key={id} job={id} onFocus={() => composer.current?.('Change: ')} />
        )}
      </Chat>
      <span class="sr-only">Job {id}</span>
    </div>
  );
}
