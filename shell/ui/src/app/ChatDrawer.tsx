export function ChatDrawer({ visible, toggle }: { visible: boolean; toggle: () => void }) {
  return visible ? (
    <aside class="drawer">
      <div class="drawer-head">
        <span>Chat</span>
        <button onClick={toggle}>Hide ›</button>
      </div>
      <div class="chat-copy">Describe what you want to analyze.</div>
    </aside>
  ) : (
    <aside class="chat-tab">
      <button onClick={toggle}>‹ Chat</button>
    </aside>
  );
}
