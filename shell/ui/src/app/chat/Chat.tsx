import { useEffect, useRef, useState } from 'preact/hooks';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import type { ChatEntry, Notification } from '../../engine/types';

const toolLabels: Record<string, string> = {
  bq_query: 'Ran a read-only query',
  bq_dry_run: 'Checked the scan size',
  bq_information_schema: 'Looked up table columns',
  data_notes: 'Read the data notes',
  brief: 'Updated the brief',
  propose_job: 'Prepared the plan',
  job_status: 'Checked the job',
  sample_results: 'Read the sample answers',
  lead_transcript: 'Read a masked transcript',
  result_aggregates: 'Summarised the results',
  write_file: 'Wrote a file',
};
function cleanTool(name?: string): string {
  const short = (name ?? '').replace(/^mcp__(app|bq)__/, '');
  return (toolLabels[short] ?? short) || 'Used a tool';
}
function errorText(error: unknown): { reason: string | null; message: string } {
  const e = EngineError.from(error);
  return { reason: e.reason, message: e.message };
}
function eventEntry(event: Record<string, unknown>, turn: string, id: number): ChatEntry | null {
  const kind = typeof event.kind === 'string' ? event.kind : '';
  const data = (event.data ?? {}) as Record<string, unknown>;
  if (kind === 'assistant_delta') {
    const delta = data.delta as Record<string, unknown> | undefined;
    return { id, ts: Date.now(), turn, role: 'assistant', text: String(delta?.text ?? '') };
  }
  if (kind === 'assistant' || kind === 'tool_use') {
    const message = (data.message ?? data) as Record<string, unknown>;
    const blocks = Array.isArray(message.content) ? message.content : [];
    const text = blocks
      .filter(
        (block): block is { type: string; text?: string } =>
          typeof block === 'object' && block !== null,
      )
      .filter((block) => block.type === 'text')
      .map((block) => block.text ?? '')
      .join('');
    const tool = blocks.find(
      (block): block is { type: string; name?: string } =>
        typeof block === 'object' && block !== null && block.type === 'tool_use',
    );
    if (tool) return { id, ts: Date.now(), turn, role: 'tool', name: tool.name, status: 'started' };
    if (text) return { id, ts: Date.now(), turn, role: 'assistant', text };
  }
  if (kind === 'tool_result') {
    const name = typeof data.name === 'string' ? data.name : undefined;
    return { id, ts: Date.now(), turn, role: 'tool', name, status: 'done' };
  }
  return null;
}

export function Chat({ project, onFocus }: { project: string; onFocus?: () => void }) {
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [more, setMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [text, setText] = useState('');
  const [ids, setIds] = useState('');
  const [attachOpen, setAttachOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<{ reason: string | null; message: string } | null>(null);
  const [attachMessage, setAttachMessage] = useState<string | null>(null);
  const [detached, setDetached] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const live = useRef(new Map<string, ChatEntry[]>());
  const nextId = useRef(-1);
  const atBottom = () => {
    const node = scroller.current;
    return !node || node.scrollHeight - node.scrollTop - node.clientHeight < 48;
  };
  const scrollLatest = () => end.current?.scrollIntoView({ block: 'end' });
  const loadHistory = async (before?: number) => {
    const result = await bridge.call('chat/history', {
      project,
      ...(before === undefined ? {} : { before: String(before) }),
      limit: 50,
    });
    setEntries((current) => {
      const merged =
        before === undefined
          ? [
              ...result.entries,
              ...current.filter(
                (entry) =>
                  entry.role !== 'user' ||
                  !result.entries.some(
                    (saved) => saved.role === 'user' && saved.text === entry.text,
                  ),
              ),
            ]
          : [...result.entries, ...current];
      const unique = new Map(merged.map((entry) => [entry.id, entry]));
      return [...unique.values()].sort((a, b) => a.id - b.id);
    });
    setMore(result.more);
    setLoading(false);
  };
  useEffect(() => {
    live.current.clear();
    setLoading(true);
    void loadHistory().catch((e) => {
      setError(errorText(e));
      setLoading(false);
    });
    const unsubscribe = bridge.onNotification((notification: Notification) => {
      if (notification.method !== 'chat/event') return;
      const params = notification.params as { project?: string; event?: Record<string, unknown> };
      if (params.project !== project || !params.event) return;
      const event = params.event;
      const turn = typeof event.turn === 'string' ? event.turn : 'live';
      const id = typeof event.entryId === 'number' ? event.entryId : nextId.current--;
      const entry = eventEntry(event, turn, id);
      if (entry) {
        const list = live.current.get(turn) ?? [];
        if (!list.some((item) => item.id === id)) {
          if (entry.role === 'assistant' && event.kind === 'assistant_delta') {
            const previous = list.find((item) => item.role === 'assistant');
            if (previous) previous.text = `${previous.text ?? ''}${entry.text ?? ''}`;
            else list.push(entry);
          } else if (entry.role === 'assistant' && event.kind === 'assistant') {
            list.splice(0, list.length, ...list.filter((item) => item.role !== 'assistant'), entry);
          } else list.push(entry);
          live.current.set(turn, list);
        }
        setEntries((current) =>
          [...current.filter((item) => item.turn !== turn), ...list].sort((a, b) => a.id - b.id),
        );
      }
      if (!(event.kind as string | undefined) && 'status' in event) {
        void loadHistory().then(() => live.current.delete(turn));
        setRunning(false);
      }
    });
    return unsubscribe;
  }, [project]);
  useEffect(() => {
    if (atBottom()) scrollLatest();
    else setDetached(true);
  }, [entries]);
  const send = async () => {
    const value = text.trim();
    if (!value || running) return;
    setText('');
    setError(null);
    setEntries((current) => [
      ...current,
      {
        id: nextId.current--,
        ts: Date.now(),
        turn: `local-${Date.now()}`,
        role: 'user',
        text: value,
      },
    ]);
    setRunning(true);
    try {
      await bridge.call('chat/send', { project, text: value });
    } catch (e) {
      setError(errorText(e));
      setRunning(false);
    }
  };
  const stop = async () => {
    await bridge.call('chat/stop', { project });
    setRunning(false);
  };
  const attach = async () => {
    try {
      const result = await bridge.call('ids/attach', { project, text: ids });
      const number = result.distinct.toLocaleString();
      const extras = [
        result.duplicates ? `${result.duplicates.toLocaleString()} duplicates removed` : '',
        result.malformed ? `${result.malformed.toLocaleString()} not valid` : '',
      ]
        .filter(Boolean)
        .join(', ');
      setAttachMessage(`${number} ids attached${extras ? ` (${extras})` : ''}`);
      setText(`I've attached a list of ${number} lead ids (${result.name}).`);
      setAttachOpen(false);
    } catch (e) {
      setAttachMessage(
        errorText(e).reason === 'too_large'
          ? 'That list is too large.'
          : errorText(e).reason === 'no_ids'
            ? 'No valid ids were found.'
            : 'Could not attach those ids.',
      );
    }
  };
  const rendered = entries;
  return (
    <section class="chat" aria-label="Claude chat">
      <div
        class="chat-scroll"
        ref={scroller}
        onScroll={() => {
          if (atBottom()) setDetached(false);
        }}
      >
        {more && (
          <button class="link-button earlier" onClick={() => void loadHistory(rendered[0]?.id)}>
            Show earlier messages
          </button>
        )}
        {loading && <p class="muted">Loading chat…</p>}
        {rendered.map((entry) =>
          entry.role === 'user' ? (
            <div class="chat-user" key={entry.id}>
              {entry.text}
            </div>
          ) : entry.role === 'tool' ? (
            <div class="chat-tool" key={entry.id}>
              <span
                class={
                  entry.status === 'failed'
                    ? 'tool-failed'
                    : entry.status === 'done'
                      ? 'tool-done'
                      : 'tool-running'
                }
              >
                {entry.status === 'failed' ? '✗' : entry.status === 'done' ? '✓' : '…'}
              </span>
              {entry.summary ?? cleanTool(entry.name)}
            </div>
          ) : entry.text ? (
            <div class="chat-assistant" key={entry.id}>
              <small>Claude</small>
              <p>{entry.text}</p>
            </div>
          ) : null,
        )}
        <div ref={end} />
      </div>
      {detached && (
        <button
          class="jump-latest"
          onClick={() => {
            scrollLatest();
            setDetached(false);
          }}
        >
          Jump to latest
        </button>
      )}
      {error && (
        <div class="chat-error" role="alert">
          {error.reason === 'claude_extra_usage_on' ? (
            <>
              Chat is off: extra usage is switched on in your Claude settings. Turn it off to chat.
              A running job is not affected.{' '}
              <button onClick={() => void bridge.call('allowance/status', {})}>Check again</button>
            </>
          ) : error.reason === 'claude_limit_reached' ? (
            `Claude's allowance is used up until ${new Date(Number((EngineError.from(error).data as { resetsAt?: number } | null)?.resetsAt ?? Date.now())).toLocaleString()}.`
          ) : (
            "Claude couldn't finish that reply. Try again."
          )}
        </div>
      )}
      {attachOpen && (
        <div class="attach-panel">
          <label for="lead-ids">One id per line, or separated by commas</label>
          <textarea
            id="lead-ids"
            value={ids}
            onInput={(event) => setIds((event.currentTarget as HTMLTextAreaElement).value)}
          />
          <button onClick={() => void attach()}>Attach</button>
        </div>
      )}
      {attachMessage && <p class="attach-message">{attachMessage}</p>}
      <form
        class="composer"
        onSubmit={(event) => {
          event.preventDefault();
          if (running) void stop();
          else void send();
        }}
      >
        <textarea
          value={text}
          placeholder="Ask Claude about this job…"
          onFocus={onFocus}
          onInput={(event) => setText((event.currentTarget as HTMLTextAreaElement).value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              if (!running) void send();
            }
          }}
        />
        <div class="composer-actions">
          <button type="button" onClick={() => setAttachOpen((value) => !value)}>
            Paste lead ids
          </button>
          <button class="primary" type="submit">
            {running ? 'Stop' : 'Send'}
          </button>
        </div>
        <small>Claude only sees masked data. Uses your Claude allowance.</small>
      </form>
    </section>
  );
}

export { cleanTool };
