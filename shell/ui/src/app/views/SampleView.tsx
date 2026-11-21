import { Chat } from '../chat/Chat';
import { SampleCard, StartCard } from '../cards/Cards';
import { allowances, selectedProject } from '../../state/store';
export function SampleView({ id, state }: { id: string; state?: string }) {
  return (
    <div class="phase-view" data-testid="sample-view">
      <Chat key={selectedProject.value ?? ''} project={selectedProject.value ?? ''} />
      {state === 'sample_ready' && <SampleCard job={id} />}
      {state === 'sample_approved' && (
        <StartCard job={id} project={selectedProject.value ?? ''} allowance={allowances.value} />
      )}
    </div>
  );
}
