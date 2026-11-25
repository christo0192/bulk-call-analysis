import { useEffect, useRef } from 'preact/hooks';
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
  const hide = useRef<HTMLButtonElement>(null);
  const show = useRef<HTMLButtonElement>(null);
  // The pressed button is replaced by its opposite; keep keyboard focus on the toggle.
  const refocus = useRef(false);
  useEffect(() => {
    if (!refocus.current) return;
    refocus.current = false;
    (visible ? hide : show).current?.focus();
  }, [visible]);
  const press = () => {
    refocus.current = true;
    toggle();
  };
  return visible ? (
    <aside class="drawer">
      <div class="drawer-head">
        <span>Chat</span>
        <button type="button" ref={hide} aria-label="Hide chat" onClick={press}>
          Hide ›
        </button>
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
      <button type="button" ref={show} aria-label="Show chat" onClick={press}>
        ‹ Chat
      </button>
    </aside>
  );
}
