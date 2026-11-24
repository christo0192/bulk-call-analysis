import { useEffect, useRef, useState } from 'preact/hooks';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import { openProjectFile } from '../../engine/shellCommands';
import type { PlanCard, Rule, RuleCondition } from '../../engine/types';

type Kind = 'in_scope' | 'threshold_group' | 'review_flag';
const operators = [
  'eq',
  'ne',
  'in',
  'not_in',
  'lt',
  'le',
  'gt',
  'ge',
  'is_null',
  'not_null',
  'contains',
];
const blank = (): RuleCondition => ({ field: '', op: 'eq', value: '' });

export function RebuildDialog({
  job,
  plan,
  onClose,
}: {
  job: string;
  plan: PlanCard;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const [rules, setRules] = useState<Rule[]>([]);
  const [currentRules, setCurrentRules] = useState(plan.rules);
  const [kind, setKind] = useState<Kind>('threshold_group');
  const [name, setName] = useState('');
  const [field, setField] = useState(plan.outputColumns[0]?.name ?? '');
  const [edges, setEdges] = useState('');
  const [labels, setLabels] = useState('');
  const [match, setMatch] = useState<'all' | 'any'>('all');
  const [conditions, setConditions] = useState<RuleCondition[]>([blank()]);
  const [error, setError] = useState<string | null>(null);
  const [rebuilt, setRebuilt] = useState<{ path: string } | null>(null);
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
      if (event.key === 'Tab' && dialog.current) {
        const items = [
          ...dialog.current.querySelectorAll<HTMLElement>('button, input, select'),
        ].filter((item) => !item.hasAttribute('disabled'));
        const first = items[0];
        const last = items[items.length - 1];
        if (first && event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (last && !event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener('keydown', handler);
    dialog.current?.querySelector<HTMLElement>('button')?.focus();
    return () => document.removeEventListener('keydown', handler);
  }, [onClose, plan.rules]);
  const update = (index: number, key: keyof RuleCondition, value: string) =>
    setConditions(
      conditions.map((item, current) => (current === index ? { ...item, [key]: value } : item)),
    );
  const addRule = () => {
    setError(null);
    if (kind === 'threshold_group') {
      const numbers = edges
        .split(',')
        .map((value) => Number(value.trim()))
        .filter(Number.isFinite);
      const names = labels
        .split(',')
        .map((value) => value.trim())
        .filter(Boolean);
      if (numbers.some((value, index) => index > 0 && value <= (numbers[index - 1] ?? -Infinity)))
        return setError('Edges must be strictly increasing.');
      if (names.length !== numbers.length + 1)
        return setError('Labels must contain one more item than edges.');
      setRules([...rules, { kind, name: name || 'Bands', field, edges: numbers, labels: names }]);
    } else {
      if (conditions.some((item) => !item.field || !item.op))
        return setError('Complete every condition.');
      setRules([
        ...rules,
        {
          kind,
          match,
          conditions: conditions.map((condition) => ({
            ...condition,
            value: typedValue(condition, plan),
          })),
          ...(kind === 'review_flag' ? { name: name || 'Review flag' } : {}),
        } as Rule,
      ]);
    }
    setName('');
    setEdges('');
    setLabels('');
    setConditions([blank()]);
  };
  const rebuild = async () => {
    try {
      setError(null);
      setRebuilt(await bridge.call('job/rebuildRules', { job, rules }));
    } catch (reason) {
      const parsed = EngineError.from(reason);
      setError(
        parsed.reason === 'invalid_rules'
          ? String((parsed.data as { detail?: unknown } | null)?.detail ?? parsed.message)
          : parsed.message,
      );
    }
  };
  return (
    <div class="modal-backdrop">
      <div
        class="rebuild-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="rebuild-title"
        ref={dialog}
      >
        <div class="dialog-head">
          <h2 id="rebuild-title">Rebuild with new rules</h2>
          <button onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <p class="dialog-note">
          Re-sorts the stored answers in seconds. No re-reading, no allowance used.
        </p>
        <h3>Current rules</h3>
        {currentRules.map((rule, index) => (
          <div class="rule-row" key={`${rule.kind}-${index}`}>
            <span>{rule.summary}</span>
            <button
              onClick={() => setCurrentRules(currentRules.filter((_, item) => item !== index))}
            >
              Remove
            </button>
          </div>
        ))}
        <h3>Add a rule</h3>
        <div class="rule-kind">
          <button class={kind === 'in_scope' ? 'selected' : ''} onClick={() => setKind('in_scope')}>
            In scope
          </button>
          <button
            class={kind === 'threshold_group' ? 'selected' : ''}
            onClick={() => setKind('threshold_group')}
          >
            Bands
          </button>
          <button
            class={kind === 'review_flag' ? 'selected' : ''}
            onClick={() => setKind('review_flag')}
          >
            Review flag
          </button>
        </div>
        {kind !== 'in_scope' && (
          <label>
            Name
            <input value={name} onInput={(event) => setName(event.currentTarget.value)} />
          </label>
        )}
        {kind === 'threshold_group' ? (
          <>
            <label>
              Numeric field
              <select value={field} onChange={(event) => setField(event.currentTarget.value)}>
                {plan.outputColumns.map((column) => (
                  <option value={column.name}>{column.name}</option>
                ))}
              </select>
            </label>
            <label>
              Edges
              <input value={edges} onInput={(event) => setEdges(event.currentTarget.value)} />
            </label>
            <label>
              Labels
              <input value={labels} onInput={(event) => setLabels(event.currentTarget.value)} />
            </label>
          </>
        ) : (
          <>
            <label>
              Match
              <select
                value={match}
                onChange={(event) => setMatch(event.currentTarget.value as 'all' | 'any')}
              >
                <option value="all">All</option>
                <option value="any">Any</option>
              </select>
            </label>
            {conditions.map((condition, index) => (
              <div class="condition-row" key={index}>
                <select
                  aria-label="Field"
                  value={condition.field}
                  onChange={(event) => update(index, 'field', event.currentTarget.value)}
                >
                  <option value="">Field</option>
                  {plan.outputColumns.map((column) => (
                    <option value={`answer.${column.name}`}>answer.{column.name}</option>
                  ))}
                </select>
                <select
                  aria-label="Operator"
                  value={condition.op}
                  onChange={(event) => update(index, 'op', event.currentTarget.value)}
                >
                  {operators.map((operator) => (
                    <option value={operator}>{operator}</option>
                  ))}
                </select>
                {!['is_null', 'not_null'].includes(condition.op) && (
                  <input
                    aria-label="Value"
                    type={inputType(condition.field, plan)}
                    value={String(condition.value ?? '')}
                    onInput={(event) => update(index, 'value', event.currentTarget.value)}
                  />
                )}
              </div>
            ))}
            <button onClick={() => setConditions([...conditions, blank()])}>Add condition</button>
          </>
        )}
        <button onClick={addRule}>Add rule</button>
        {error && (
          <p class="dialog-error" role="alert">
            {error}
          </p>
        )}
        {rebuilt && (
          <p class="rebuild-success">
            Rebuilt: {rebuilt.path}{' '}
            <button onClick={() => void openProjectFile(plan.project, rebuilt.path)}>Open</button>
          </p>
        )}
        <div class="dialog-actions">
          <button onClick={onClose}>Cancel</button>
          <button class="primary" onClick={() => void rebuild()}>
            Rebuild
          </button>
        </div>
      </div>
    </div>
  );
}
function inputType(field: string, plan: PlanCard) {
  const type = plan.outputColumns.find((column) => `answer.${column.name}` === field)?.type;
  return type === 'number' ? 'number' : type === 'boolean' ? 'text' : 'text';
}
function typedValue(condition: RuleCondition, plan: PlanCard) {
  const type = plan.outputColumns.find(
    (column) => `answer.${column.name}` === condition.field,
  )?.type;
  if (type === 'number' && condition.value !== '') return Number(condition.value);
  if (type === 'boolean') return String(condition.value).toLowerCase() === 'true';
  return condition.value;
}
