import { Chat } from '../chat/Chat';
import { SampleCard, StartCard } from '../cards/Cards';
import { allowances, selectedProject } from '../../state/store';
import { useRef } from 'preact/hooks';
export function SampleView({ id, state }: { id: string; state?: string }) {
  const composer = useRef<((prefix?: string) => void) | null>(null);
  return (
    <div class="phase-view" data-testid="sample-view">
      <Chat
        key={selectedProject.value ?? ''}
        project={selectedProject.value ?? ''}
        composer={composer}
      >
        {state === 'sample_ready' && (
          <SampleCard key={id} job={id} onFocus={() => composer.current?.('Change: ')} />
        )}
        {state === 'sample_approved' && (
          <StartCard
            key={id}
            job={id}
            project={selectedProject.value ?? ''}
            allowance={allowances.value}
          />
        )}
      </Chat>
    </div>
  );
}
