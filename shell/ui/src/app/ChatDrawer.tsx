import { Chat } from './chat/Chat';
import { selectedProject } from '../state/store';
export function ChatDrawer({ visible, toggle }: { visible: boolean; toggle: () => void }) {
  return visible ? (
    <aside class="drawer">
      <div class="drawer-head">
        <span>Chat</span>
        <button onClick={toggle}>Hide ›</button>
      </div>
      <Chat project={selectedProject.value ?? ''} />
    </aside>
  ) : (
    <aside class="chat-tab">
      <button onClick={toggle}>‹ Chat</button>
    </aside>
  );
}
