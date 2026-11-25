import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { beforeEach, expect, it, vi } from 'vitest';
import { cleanTool } from './Chat';
import { Chat } from './Chat';
import { mockBridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';

const entry = (id: number, role: 'user' | 'assistant' | 'status', text: string, turn = 't1') => ({
  id,
  ts: id,
  turn,
  role,
  text,
});

beforeEach(() => {
  mockBridge.calls = [];
  mockBridge.clearResponses();
  mockBridge.setResponse('chat/history', async () => ({ entries: [], more: false }));
  mockBridge.setResponse('chat/send', async () => ({ status: 'completed', project: 'p1' }));
});

function chat() {
  return render(<Chat project="p1" />);
}

function event(kind: string, data: Record<string, unknown>, turn = 'live') {
  mockBridge.emit({
    jsonrpc: '2.0',
    method: 'chat/event',
    params: { project: 'p1', event: { kind, turn, data } },
  });
}

it('labels bq queries', () => expect(cleanTool('bq_query')).toBe('Ran a read-only query'));
it('labels dry runs', () => expect(cleanTool('bq_dry_run')).toBe('Checked the scan size'));
it('labels information schema reads', () =>
  expect(cleanTool('bq_information_schema')).toBe('Looked up table columns'));
it('labels data notes', () => expect(cleanTool('data_notes')).toBe('Read the data notes'));
it('labels brief updates', () => expect(cleanTool('brief')).toBe('Updated the brief'));
it('labels proposed jobs', () => expect(cleanTool('propose_job')).toBe('Prepared the plan'));
it('labels job status checks', () => expect(cleanTool('job_status')).toBe('Checked the job'));
it('labels sample reads', () =>
  expect(cleanTool('sample_results')).toBe('Read the sample answers'));
it('labels transcript reads', () =>
  expect(cleanTool('lead_transcript')).toBe('Read a masked transcript'));
it('labels result aggregates', () =>
  expect(cleanTool('result_aggregates')).toBe('Summarised the results'));
it('strips app tool prefixes', () =>
  expect(cleanTool('mcp__app__write_file')).toBe('Wrote a file'));
it('strips bq tool prefixes', () =>
  expect(cleanTool('mcp__bq__bq_query')).toBe('Ran a read-only query'));
it('uses a safe label for unknown tools', () =>
  expect(cleanTool('unknown_tool')).toBe('unknown_tool'));
it('uses a safe label for missing tools', () => expect(cleanTool()).toBe('Used a tool'));

it('two turns render in order with no duplicates after reload', async () => {
  const history = [
    entry(1, 'user', 'first'),
    entry(2, 'assistant', 'reply one'),
    entry(3, 'user', 'second'),
    entry(4, 'assistant', 'reply two'),
  ];
  mockBridge.setResponse('chat/history', async () => ({ entries: history, more: false }));
  const view = chat();
  await waitFor(() => expect(screen.getAllByText(/reply/)).toHaveLength(2));
  expect(screen.getAllByText(/reply/).map((item) => item.textContent)).toEqual([
    'reply one',
    'reply two',
  ]);
  view.unmount();
  chat();
  await waitFor(() => expect(screen.getAllByText(/reply/)).toHaveLength(2));
});

it('streamed text accumulates across many deltas with the same entryId', async () => {
  chat();
  event('assistant_delta', { delta: { text: 'one ' } });
  event('assistant_delta', { delta: { text: 'two ' } });
  event('assistant_delta', { delta: { text: 'three' } });
  expect(await screen.findByText('one two three')).toBeInTheDocument();
  expect(screen.getAllByText('one two three')).toHaveLength(1);
});

it('text after a tool call shows live', async () => {
  chat();
  event('tool_use', { message: { content: [{ type: 'tool_use', id: 'x', name: 'bq_query' }] } });
  event('assistant_delta', { delta: { text: 'after tool' } });
  expect(await screen.findByText('after tool')).toBeInTheDocument();
});

it('stop mid-stream keeps the user message and the partial reply', async () => {
  mockBridge.setResponse('chat/send', async () => new Promise(() => undefined));
  chat();
  fireEvent.input(screen.getByRole('textbox'), { target: { value: 'question' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  event('assistant_delta', { delta: { text: 'partial' } });
  fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
  expect(screen.getByText('question')).toBeInTheDocument();
  expect(screen.getByText('partial')).toBeInTheDocument();
  expect(mockBridge.calls).toContainEqual({ method: 'chat/stop', params: { project: 'p1' } });
});

it('tool line starts on tool_use and completes on the matching tool_result', async () => {
  chat();
  event('tool_use', { message: { content: [{ type: 'tool_use', id: 'x', name: 'bq_query' }] } });
  const row = await screen.findByText('Ran a read-only query');
  expect(row.querySelector('.tool-running')).not.toBeNull();
  event('tool_result', { message: { content: [{ type: 'tool_result', tool_use_id: 'x' }] } });
  await waitFor(() => expect(row.querySelector('.tool-done')).not.toBeNull());
});

it('missing chat history shows no error and keeps live items', async () => {
  mockBridge.setResponse('chat/history', async () => {
    throw 'RPC error -32601: Method not found';
  });
  chat();
  event('assistant_delta', { delta: { text: 'still live' } });
  expect(await screen.findByText('still live')).toBeInTheDocument();
  expect(screen.queryByRole('alert')).toBeNull();
});

it('consecutive turns without history stay separate', async () => {
  chat();
  event('assistant_delta', { delta: { text: 'first' } }, 'turn-1');
  event('assistant_delta', { delta: { text: 'second' } }, 'turn-2');
  expect(await screen.findByText('first')).toBeInTheDocument();
  expect(screen.getByText('second')).toBeInTheDocument();
});

it('show earlier sends the smallest persisted id as a number', async () => {
  mockBridge.setResponse('chat/history', async (params) =>
    (params as { before?: number }).before === undefined
      ? { entries: [entry(8, 'user', 'new'), entry(11, 'assistant', 'new reply')], more: true }
      : { entries: [entry(2, 'assistant', 'old')], more: false },
  );
  chat();
  fireEvent.click(await screen.findByRole('button', { name: 'Show earlier messages' }));
  await waitFor(() => expect(screen.getByText('old')).toBeInTheDocument());
  expect(mockBridge.calls).toContainEqual({
    method: 'chat/history',
    params: { project: 'p1', before: 8, limit: 50 },
  });
});

it("switching projects shows only that project's chat", async () => {
  const view = chat();
  event('assistant_delta', { delta: { text: 'project one' } });
  view.rerender(<Chat project="p2" />);
  expect(screen.queryByText('project one')).toBeNull();
  mockBridge.emit({
    jsonrpc: '2.0',
    method: 'chat/event',
    params: {
      project: 'p2',
      event: { kind: 'assistant_delta', turn: 'p2', data: { delta: { text: 'project two' } } },
    },
  });
  expect(await screen.findByText('project two')).toBeInTheDocument();
});

it('a failed turn leaves no stale bubble and restores the text once', async () => {
  mockBridge.setResponse('chat/send', async () => {
    event('assistant_delta', { delta: { text: 'partial' } }, 'engine-turn');
    throw new Error('offline');
  });
  chat();
  fireEvent.input(screen.getByRole('textbox'), { target: { value: 'retry me' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  await waitFor(() => expect(screen.getByRole('textbox')).toHaveValue('retry me'));
  expect(screen.queryByText('retry me', { selector: '.chat-user' })).toBeNull();
  expect(screen.queryByText('partial')).toBeNull();
});

it('remounting mid-turn does not duplicate items after the turn is persisted', async () => {
  let persisted = false;
  mockBridge.setResponse('chat/history', async () =>
    persisted
      ? {
          entries: [
            entry(1, 'user', 'question', 'turn-1'),
            entry(2, 'assistant', 'answer', 'turn-1'),
          ],
          more: false,
        }
      : { entries: [], more: false },
  );
  const view = chat();
  event('assistant_delta', { delta: { text: 'partial' } }, 'turn-1');
  view.unmount();
  chat();
  event('assistant_delta', { delta: { text: 'answer' } }, 'turn-1');
  persisted = true;
  mockBridge.emit({
    jsonrpc: '2.0',
    method: 'chat/event',
    params: { project: 'p1', event: { status: 'completed' } },
  });
  await waitFor(() => expect(screen.getAllByText('answer')).toHaveLength(1));
});

it("the shell's RPC timed out keeps the turn running without a failure banner", async () => {
  mockBridge.setResponse('chat/send', async () => {
    event('assistant_delta', { delta: { text: 'still working' } }, 'engine-turn');
    throw new EngineError(null, 'RPC timed out');
  });
  chat();
  fireEvent.input(screen.getByRole('textbox'), { target: { value: 'wait' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument());
  expect(screen.queryByRole('alert')).toBeNull();
});

it('a chat mounted mid-turn shows Stop', async () => {
  chat();
  event('assistant_delta', { delta: { text: 'streaming reply' } }, 'engine-turn');
  expect(await screen.findByRole('button', { name: 'Stop' })).toBeInTheDocument();
  expect(screen.getByText('streaming reply')).toBeInTheDocument();
});

it('the end event without a turn id ends the running state', async () => {
  chat();
  event('assistant_delta', { delta: { text: 'finished reply' } }, 'engine-turn');
  expect(await screen.findByRole('button', { name: 'Stop' })).toBeInTheDocument();
  mockBridge.emit({
    jsonrpc: '2.0',
    method: 'chat/event',
    params: { project: 'p1', event: { status: 'completed' } },
  });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument());
  expect(screen.getByText('finished reply')).toBeInTheDocument();
});

it('a resolved chat/send ends the running state even without an end event', async () => {
  let completed = false;
  mockBridge.setResponse('chat/history', async () =>
    completed
      ? {
          entries: [
            entry(1, 'user', 'resolved question', 'engine-turn'),
            entry(2, 'assistant', 'resolved reply', 'engine-turn'),
          ],
          more: false,
        }
      : { entries: [], more: false },
  );
  mockBridge.setResponse('chat/send', async () => {
    // A real turn always streams at least its `system` event with the engine's turn id,
    // which links the optimistic message to that turn, and then resolves.
    event('system', {}, 'engine-turn');
    completed = true;
    return { status: 'completed', project: 'p1' };
  });
  chat();
  fireEvent.input(screen.getByRole('textbox'), { target: { value: 'resolved question' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument());
  expect(screen.getByText('resolved reply')).toBeInTheDocument();
  expect(screen.getAllByText('resolved question')).toHaveLength(1);
});

it('a rejected send ends running unless it is a timeout', async () => {
  mockBridge.setResponse('chat/send', async () => {
    throw new Error('offline');
  });
  chat();
  fireEvent.input(screen.getByRole('textbox'), { target: { value: 'try again' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument());
  expect(screen.getByRole('alert')).toBeInTheDocument();
});

it('sending during a running turn shows a gentle notice and keeps the reply', async () => {
  mockBridge.setResponse('chat/send', async () => {
    event('assistant_delta', { delta: { text: 'reply in progress' } }, 'engine-turn');
    throw new EngineError(-32001, 'Chat turn is already running');
  });
  chat();
  fireEvent.input(screen.getByRole('textbox'), { target: { value: 'another question' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  expect(await screen.findByText('reply in progress')).toBeInTheDocument();
  expect(
    screen.getByText('Claude is still replying. Wait for it or press Stop.'),
  ).toBeInTheDocument();
  expect(screen.queryByText("Claude couldn't finish that reply. Try again.")).toBeNull();
});

it('a long history opens scrolled to the latest message', async () => {
  mockBridge.setResponse('chat/history', async () => ({
    entries: Array.from({ length: 60 }, (_, i) =>
      entry(i + 1, i % 2 ? 'assistant' : 'user', `message ${i}`),
    ),
    more: false,
  }));
  chat();
  await screen.findByText('message 59');
  expect(screen.queryByRole('button', { name: 'Jump to latest' })).toBeNull();
});

it('status entries are not labelled as Claude', async () => {
  mockBridge.setResponse('chat/history', async () => ({
    entries: [entry(1, 'status', 'Turn ended')],
    more: false,
  }));
  chat();
  expect(await screen.findByText('Turn ended')).toBeInTheDocument();
  expect(screen.queryByText('Claude')).toBeNull();
});

it('a finished reply is announced once to screen readers, not token by token', async () => {
  const view = chat();
  const region = () => view.container.querySelector('.chat [aria-live="polite"]');
  event('assistant_delta', { delta: { text: 'first answer' } }, 'turn-a');
  expect(region()?.textContent).toBe('');
  mockBridge.emit({
    jsonrpc: '2.0',
    method: 'chat/event',
    params: { project: 'p1', event: { status: 'completed' } },
  });
  await waitFor(() => expect(region()?.textContent).toBe('Claude replied'));
  event('assistant_delta', { delta: { text: 'second answer' } }, 'turn-b');
  mockBridge.emit({
    jsonrpc: '2.0',
    method: 'chat/event',
    params: { project: 'p1', event: { status: 'completed' } },
  });
  // The text changes so the second reply is read out too.
  await waitFor(() => expect(region()?.textContent).toBe('Claude replied.'));
});
