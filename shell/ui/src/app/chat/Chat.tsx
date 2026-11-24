import { useEffect, useRef, useState } from 'preact/hooks';
import type { ComponentChildren } from 'preact';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import { formatCount } from '../format';
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
type ChatError = { reason: string | null; message: string; code: number | null; data: unknown };
function errorText(error: unknown): ChatError {
  const e = EngineError.from(error);
  return { reason: e.reason, message: e.message, code: e.code, data: e.data };
}
function hasReason(error: ChatError, reason: string): boolean {
  // The current shell drops data.reason; retain message matching until that shell slice lands.
  return (
    error.reason === reason || error.message.toLowerCase().includes(reason.replaceAll('_', ' '))
  );
}
function blocks(event: Record<string, unknown>): Record<string, unknown>[] {
  const data = (event.data ?? {}) as Record<string, unknown>;
  const message = (data.message ?? data) as Record<string, unknown>;
  return Array.isArray(message.content)
    ? message.content.filter(
        (item): item is Record<string, unknown> => typeof item === 'object' && item !== null,
      )
    : [];
}
function historyTool(entry: ChatEntry): string {
  return cleanTool(entry.name ?? entry.summary?.replace(/\s+(started|done|failed)$/, ''));
}
function historyMissing(error: unknown): boolean {
  return errorText(error).code === -32601;
}
function isAlreadyRunning(error: ChatError): boolean {
  return /chat turn is already running/i.test(error.message);
}

export function Chat({
  project,
  composer,
  composerRequest,
  onComposerRequestHandled,
  children,
}: {
  project: string;
  composer?: { current: ((prefix?: string) => void) | null };
  composerRequest?: { prefix: string; id: number } | null;
  onComposerRequestHandled?: () => void;
  children?: ComponentChildren;
}) {
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [more, setMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [text, setText] = useState('');
  const [ids, setIds] = useState('');
  const [attachOpen, setAttachOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<ChatError | null>(null);
  const [attachMessage, setAttachMessage] = useState<string | null>(null);
  const [earlierError, setEarlierError] = useState(false);
  const [detached, setDetached] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const live = useRef(new Map<string, ChatEntry[]>());
  const persisted = useRef<ChatEntry[]>([]);
  const nextId = useRef(-1);
  const activeTurn = useRef<string | null>(null);
  const turnAliases = useRef(new Map<string, string>());
  const endedTurns = useRef(new Set<string>());
  const restoredTurns = useRef(new Set<string>());
  const [attachPending, setAttachPending] = useState(false);
  const wasAtBottom = useRef(true);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const allLive = () => [...live.current.values()].flat();
  if (composer)
    composer.current = (prefix = '') => {
      setText((current) => (current.startsWith(prefix) ? current : `${prefix}${current}`));
      textarea.current?.focus();
    };
  useEffect(() => {
    if (!composerRequest) return;
    setText((current) =>
      current.startsWith(composerRequest.prefix) ? current : `${composerRequest.prefix}${current}`,
    );
    textarea.current?.focus();
    onComposerRequestHandled?.();
  }, [composerRequest, onComposerRequestHandled]);
  const publish = () => setEntries([...persisted.current, ...allLive()]);
  const scrollLatest = () => end.current?.scrollIntoView({ block: 'end' });
  const loadHistory = async (before?: number): Promise<boolean> => {
    try {
      const result = await bridge.call('chat/history', {
        project,
        ...(before === undefined ? {} : { before }),
        limit: 50,
      });
      const combined =
        before === undefined
          ? [...persisted.current, ...result.entries]
          : [...result.entries, ...persisted.current];
      persisted.current = [...new Map(combined.map((entry) => [entry.id, entry])).values()].sort(
        (a, b) => a.id - b.id,
      );
      const persistedTurns = new Set(persisted.current.map((entry) => entry.turn));
      for (const turn of live.current.keys()) {
        if (persistedTurns.has(turn)) live.current.delete(turn);
      }
      setMore(result.more);
      publish();
      setLoading(false);
      return true;
    } catch (reason) {
      if (!historyMissing(reason)) throw reason;
      setLoading(false);
      return false;
    }
  };
  const reconcileEnd = async (turnHint: string | null) => {
    const candidates = [...live.current.keys()];
    if (activeTurn.current && !candidates.includes(activeTurn.current)) {
      candidates.push(activeTurn.current);
    }
    const turn =
      turnHint ?? [...candidates].reverse().find((candidate) => !endedTurns.current.has(candidate));
    if (!turn || endedTurns.current.has(turn)) {
      setRunning(false);
      return;
    }
    endedTurns.current.add(turn);
    if (activeTurn.current === turn) activeTurn.current = null;
    setRunning(false);
    const aliases = [turn, turnAliases.current.get(turn)].filter(
      (value): value is string => value !== undefined,
    );
    try {
      const available = await loadHistory();
      const persistedTurn = persisted.current.some((entry) => aliases.includes(entry.turn));
      if (available && persistedTurn) aliases.forEach((alias) => live.current.delete(alias));
      publish();
    } catch (reason) {
      setError(errorText(reason));
    }
  };
  useEffect(() => {
    let active = true;
    live.current.clear();
    persisted.current = [];
    activeTurn.current = null;
    endedTurns.current.clear();
    setEntries([]);
    setLoading(true);
    void loadHistory().catch((reason) => {
      if (active) {
        setError(errorText(reason));
        setLoading(false);
      }
    });
    const unsubscribe = bridge.onNotification((notification: Notification) => {
      if (!active || notification.method !== 'chat/event') return;
      const params = notification.params as { project?: string; event?: Record<string, unknown> };
      if (params.project !== project || !params.event) return;
      const event = params.event;
      const kind = typeof event.kind === 'string' ? event.kind : '';
      const eventTurn = typeof event.turn === 'string' ? event.turn : undefined;
      const receivedTurn = eventTurn ?? activeTurn.current;
      if (!kind && 'status' in event) {
        void reconcileEnd(eventTurn ?? activeTurn.current);
        return;
      }
      if (!receivedTurn) return;
      const turn = receivedTurn;
      if (!endedTurns.current.has(turn)) setRunning(true);
      if (activeTurn.current?.startsWith('local-') && activeTurn.current !== receivedTurn) {
        const localTurn = activeTurn.current;
        const pending = live.current.get(localTurn) ?? [];
        if (live.current.has(receivedTurn)) {
          live.current.delete(localTurn);
        } else {
          turnAliases.current.set(localTurn, receivedTurn);
          live.current.delete(localTurn);
          live.current.set(
            receivedTurn,
            pending.map((entry) => ({ ...entry, turn: receivedTurn })),
          );
        }
        activeTurn.current = receivedTurn;
      }
      activeTurn.current = receivedTurn;
      const list = live.current.get(turn) ?? [];
      const id = nextId.current--;
      const data = (event.data ?? {}) as Record<string, unknown>;
      const content = blocks(event);
      if (kind === 'assistant_delta') {
        const delta = (data.delta ?? {}) as Record<string, unknown>;
        const lastTool = Math.max(
          -1,
          ...list.map((item, index) => (item.role === 'tool' ? index : -1)),
        );
        const previous = [...list.slice(lastTool + 1)]
          .reverse()
          .find((item: ChatEntry) => item.role === 'assistant');
        if (previous) previous.text = `${previous.text ?? ''}${String(delta.text ?? '')}`;
        else
          list.push({
            id,
            ts: Date.now(),
            turn,
            role: 'assistant',
            text: String(delta.text ?? ''),
          });
      } else if (kind === 'assistant') {
        const message = content
          .filter((item) => item.type === 'text')
          .map((item) => String(item.text ?? ''))
          .join('');
        if (message) {
          const lastTool = Math.max(
            -1,
            ...list.map((item, index) => (item.role === 'tool' ? index : -1)),
          );
          const previous = [...list.slice(lastTool + 1)]
            .reverse()
            .find((item: ChatEntry) => item.role === 'assistant');
          if (previous) previous.text = message;
          else list.push({ id, ts: Date.now(), turn, role: 'assistant', text: message });
        }
      } else if (kind === 'tool_use') {
        content
          .filter((item) => item.type === 'tool_use')
          .forEach((item) =>
            list.push({
              id: nextId.current--,
              ts: Date.now(),
              turn,
              role: 'tool',
              name: typeof item.name === 'string' ? item.name : undefined,
              toolId: String(item.id ?? ''),
              status: 'started',
            }),
          );
      } else if (kind === 'tool_result') {
        const item = content.find((value) => value.type === 'tool_result') ?? data;
        const toolId = String(item.tool_use_id ?? '');
        const matching = list.find((entry) => entry.role === 'tool' && entry.toolId === toolId);
        const failed = Boolean(item.is_error ?? item.isError ?? data.is_error ?? data.isError);
        if (matching) matching.status = failed ? 'failed' : 'done';
        else
          list.push({
            id,
            ts: Date.now(),
            turn,
            role: 'tool',
            toolId,
            status: failed ? 'failed' : 'done',
          });
      }
      if (
        kind === 'assistant_delta' ||
        kind === 'assistant' ||
        kind === 'tool_use' ||
        kind === 'tool_result'
      ) {
        live.current.set(turn, list);
        publish();
      }
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, [project]);
  useEffect(() => {
    if (wasAtBottom.current) {
      scrollLatest();
      setDetached(false);
    } else setDetached(true);
  }, [entries]);
  const send = async () => {
    const value = text.trim();
    if (!value || running) return;
    const turn = `local-${Date.now()}-${Math.random()}`;
    restoredTurns.current.delete(turn);
    activeTurn.current = turn;
    live.current.set(turn, [
      { id: nextId.current--, ts: Date.now(), turn, role: 'user', text: value },
    ]);
    publish();
    setText('');
    setError(null);
    setRunning(true);
    try {
      await bridge.call('chat/send', { project, text: value });
      await reconcileEnd(activeTurn.current);
    } catch (reason) {
      const parsed = errorText(reason);
      const engineTimeout =
        /timed out/i.test(parsed.message) && !/write timed out/i.test(parsed.message);
      if (isAlreadyRunning(parsed)) {
        live.current.delete(turn);
        publish();
        setText((current) => current || value);
        setRunning(true);
        setError(parsed);
      } else if (!engineTimeout) {
        const engineTurn = turnAliases.current.get(turn);
        live.current.delete(turn);
        if (engineTurn) live.current.delete(engineTurn);
        publish();
        if (!restoredTurns.current.has(turn)) {
          restoredTurns.current.add(turn);
          setText((current) => current || value);
        }
        setRunning(false);
        setError(parsed);
      } else {
        setError(null);
      }
    }
  };
  const stop = async () => {
    try {
      await bridge.call('chat/stop', { project });
      setRunning(false);
    } catch (reason) {
      setError(errorText(reason));
    }
  };
  const attach = async () => {
    if (attachPending) return;
    setAttachPending(true);
    try {
      const result = await bridge.call('ids/attach', { project, text: ids });
      const number = formatCount(result.distinct);
      const extras = [
        result.duplicates ? `${formatCount(result.duplicates)} duplicates removed` : '',
        result.malformed ? `${formatCount(result.malformed)} not valid` : '',
      ]
        .filter(Boolean)
        .join(', ');
      setAttachMessage(`${number} ids attached${extras ? ` (${extras})` : ''}`);
      setText(`I've attached a list of ${number} lead ids (${result.name}).`);
      setAttachOpen(false);
    } catch (reason) {
      const parsed = errorText(reason);
      setAttachMessage(
        parsed.reason === 'too_large'
          ? 'That list is too large.'
          : parsed.reason === 'no_ids'
            ? 'No valid ids were found.'
            : parsed.code === -32601
              ? "Attaching lead lists isn't available yet"
              : 'Could not attach those ids.',
      );
    } finally {
      setAttachPending(false);
    }
  };
  const persistedIds = persisted.current.filter((entry) => entry.id > 0).map((entry) => entry.id);
  const rendered = entries.filter((entry, index) => {
    if (entry.role !== 'tool' || entry.status !== 'started') return true;
    const label = historyTool(entry);
    return !entries
      .slice(index + 1)
      .some(
        (later) =>
          later.role === 'tool' && later.status !== 'started' && historyTool(later) === label,
      );
  });
  return (
    <section class="chat" aria-label="Claude chat">
      <div
        class="chat-scroll"
        ref={scroller}
        onScroll={() => {
          wasAtBottom.current =
            !scroller.current ||
            scroller.current.scrollHeight -
              scroller.current.scrollTop -
              scroller.current.clientHeight <
              48;
          if (wasAtBottom.current) setDetached(false);
        }}
      >
        {more && (
          <button
            class="link-button earlier"
            onClick={() => {
              setEarlierError(false);
              void loadHistory(Math.min(...persistedIds)).catch(() => setEarlierError(true));
            }}
          >
            Show earlier messages
          </button>
        )}
        {earlierError && <p class="muted">Couldn't load earlier messages. Try again</p>}
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
              {historyTool(entry)}
            </div>
          ) : entry.role === 'status' ? (
            <div class="chat-tool" key={entry.id}>
              {entry.text?.startsWith('Turn stopped') ? 'Turn stopped' : 'Turn ended'}
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
            wasAtBottom.current = true;
            setDetached(false);
          }}
        >
          Jump to latest
        </button>
      )}
      {error && (
        <div class="chat-error" role="alert">
          {isAlreadyRunning(error) ? (
            'Claude is still replying. Wait for it or press Stop.'
          ) : hasReason(error, 'claude_extra_usage_on') ? (
            <>
              Chat is off: extra usage is switched on in your Claude settings. Turn it off to chat.
              A running job is not affected.{' '}
              <button
                onClick={() =>
                  void bridge
                    .call('allowance/status', {})
                    .catch((reason) => setError(errorText(reason)))
                }
              >
                Check again
              </button>
            </>
          ) : hasReason(error, 'claude_limit_reached') ? (
            (error.data as { resetsAt?: number } | null)?.resetsAt == null ? (
              "Claude's allowance is used up for now. Try again later."
            ) : (
              `Claude's allowance is used up until ${new Date(Number((error.data as { resetsAt: number }).resetsAt) * ((error.data as { resetsAt: number }).resetsAt < 1e12 ? 1000 : 1)).toLocaleString()}.`
            )
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
          <button disabled={attachPending} onClick={() => void attach()}>
            Attach
          </button>
        </div>
      )}
      {attachMessage && <p class="attach-message">{attachMessage}</p>}
      {children}
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
          ref={textarea}
          placeholder="Ask Claude about this job…"
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
