import { Chat } from './chat/Chat';
import { selectedProject } from '../state/store';
export function ChatDrawer({
  visible,
  toggle,
  composer,
  composerRequest,
  onComposerRequestHandled,
}: {
  visible: boolean;
  toggle: () => void;
  composer?: { current: ((prefix?: string) => void) | null };
  composerRequest?: { prefix: string; id: number } | null;
  onComposerRequestHandled?: () => void;
}) {
  return visible ? (
    <aside class="drawer">
      <div class="drawer-head">
        <span>Chat</span>
        <button onClick={toggle}>Hide ›</button>
      </div>
      <Chat
        key={selectedProject.value ?? ''}
        project={selectedProject.value ?? ''}
        composer={composer}
        composerRequest={composerRequest}
        onComposerRequestHandled={onComposerRequestHandled}
      />
    </aside>
  ) : (
    <aside class="chat-tab">
      <button onClick={toggle}>‹ Chat</button>
    </aside>
  );
}
