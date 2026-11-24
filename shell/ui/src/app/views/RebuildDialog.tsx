import { useEffect, useRef, useState } from 'preact/hooks';
import { bridge } from '../../engine/bridge';
import { EngineError } from '../../engine/client';
import { openProjectFile } from '../../engine/shellCommands';
import type {
  PlanCard,
  Rule,
  RuleCondition,
  PlanRule,
  OutputColumn,
  RebuildResult,
} from '../../engine/types';

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
  onRebuilt,
}: {
  job: string;
  plan: PlanCard;
  onClose: () => void;
  onRebuilt?: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const opener = useRef<Element | null>(null);
  const [rules, setRules] = useState<Rule[]>(
    plan.rules.flatMap((rule) => (rule.definition ? [rule.definition] : [])),
  );
  const [kind, setKind] = useState<Kind>('threshold_group');
  const [name, setName] = useState('');
  const [field, setField] = useState(
    plan.outputColumns[0] ? `answer.${plan.outputColumns[0].name}` : '',
  );
  const [edges, setEdges] = useState('');
  const [labels, setLabels] = useState('');
  const [match, setMatch] = useState<'all' | 'any'>('all');
  const [conditions, setConditions] = useState<RuleCondition[]>([blank()]);
  const [error, setError] = useState<string | null>(null);
  const [rebuilt, setRebuilt] = useState<RebuildResult | null>(null);
  const [pending, setPending] = useState(false);
  const [confirmEmpty, setConfirmEmpty] = useState(false);
  const firstJob = useRef(job);
  const hasNumberColumn = plan.outputColumns.some((column) => numeric(column.type));
  useEffect(() => {
    opener.current = document.activeElement;
    const focusFirst = requestAnimationFrame(() =>
      dialog.current?.querySelector<HTMLElement>('input, select, button')?.focus(),
    );
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== 'Tab' || !dialog.current) return;
      const items = [
        ...dialog.current.querySelectorAll<HTMLElement>('button, input, select, textarea'),
      ].filter(
        (item) => !(item as HTMLButtonElement | HTMLInputElement | HTMLSelectElement).disabled,
      );
      const first = items[0];
      const last = items[items.length - 1];
      if (
        first &&
        last &&
        ((event.shiftKey && document.activeElement === first) ||
          (!event.shiftKey && document.activeElement === last))
      ) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      }
    };
    document.addEventListener('keydown', handler);
    return () => {
      cancelAnimationFrame(focusFirst);
      document.removeEventListener('keydown', handler);
      opener.current instanceof HTMLElement && opener.current.focus();
    };
  }, [onClose]);
  useEffect(() => {
    if (firstJob.current !== job) onClose();
  }, [job, onClose]);
  const close = () => onClose();
  const update = (index: number, key: keyof RuleCondition, value: string) =>
    setConditions((items) =>
      items.map((item, current) => (current === index ? { ...item, [key]: value } : item)),
    );
  const addRule = () => {
    setError(null);
    if (kind === 'threshold_group') {
      const rawEdges = edges.split(',').map((value) => value.trim());
      if (rawEdges.some((value) => !value || !Number.isFinite(Number(value))))
        return setError(
          `Edge "${rawEdges.find((value) => !value || !Number.isFinite(Number(value))) ?? ''}" is not a finite number.`,
        );
      const numbers = rawEdges.map(Number);
      if (numbers.some((value, index) => index > 0 && value <= numbers[index - 1]!))
        return setError('Edges must be strictly increasing.');
      const names = labels.split(',').map((value) => value.trim());
      if (names.some((value) => !value) || names.length !== numbers.length + 1)
        return setError('Labels must contain one more non-empty item than edges.');
      if (new Set(names).size !== names.length) return setError('Labels must be unique.');
      setRules((items) => [
        ...items,
        { kind, name: name || 'Bands', field, edges: numbers, labels: names },
      ]);
    } else {
      if (conditions.some((item) => !item.field || !item.op))
        return setError('Complete every condition.');
      const definition = {
        kind,
        ...(kind === 'review_flag' ? { name: name || 'Review flag' } : {}),
        match,
        conditions: conditions.map((condition) => typedCondition(condition, plan)),
      } as Rule;
      setRules((items) => [...items, definition]);
    }
    setName('');
    setEdges('');
    setLabels('');
    setConditions([blank()]);
  };
  const rebuild = async () => {
    if (!rules.length && !confirmEmpty) {
      setConfirmEmpty(true);
      return;
    }
    setPending(true);
    setError(null);
    try {
      const result = await bridge.call('job/rebuildRules', { job, rules });
      setRebuilt(result);
      onRebuilt?.();
    } catch (reason) {
      const parsed = EngineError.from(reason);
      const detail = (parsed.data as { detail?: unknown } | null)?.detail;
      setError(
        parsed.reason === 'invalid_rules' && typeof detail === 'string' ? detail : parsed.message,
      );
    } finally {
      setPending(false);
    }
  };
  const currentInScope = rules.some((rule) => rule.kind === 'in_scope');
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
          <button onClick={close} aria-label="Close">
            ×
          </button>
        </div>
        <p class="dialog-note">
          Re-sorts the stored answers in seconds. No re-reading, no allowance used.
        </p>
        <h3>Current rules</h3>
        {rules.map((rule, index) => (
          <div class="rule-row" key={`${rule.kind}-${index}`} title={ruleTitle(rule)}>
            <span>{ruleTitle(rule)}</span>
            <button onClick={() => setRules((items) => items.filter((_, item) => item !== index))}>
              Remove
            </button>
          </div>
        ))}
        <h3>Add a rule</h3>
        <div class="rule-kind">
          <button
            disabled={currentInScope}
            title={currentInScope ? 'Only one in-scope rule is allowed' : undefined}
            class={kind === 'in_scope' ? 'selected' : ''}
            onClick={() => setKind('in_scope')}
          >
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
            {hasNumberColumn ? (
              <>
                <label>
                  Numeric field
                  <select value={field} onChange={(event) => setField(event.currentTarget.value)}>
                    {plan.outputColumns
                      .filter((column) => numeric(column.type))
                      .map((column) => (
                        <option value={`answer.${column.name}`}>{column.name}</option>
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
              <p class="dialog-note">Bands need a number column; this job has none.</p>
            )}
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
              <ConditionRow
                key={index}
                condition={condition}
                plan={plan}
                onChange={(key, value) => update(index, key, value)}
              />
            ))}
            <button onClick={() => setConditions((items) => [...items, blank()])}>
              Add condition
            </button>
          </>
        )}
        <button disabled={kind === 'threshold_group' && !hasNumberColumn} onClick={addRule}>
          Add rule
        </button>
        {error && (
          <p class="dialog-error" role="alert">
            {error}
          </p>
        )}
        {confirmEmpty && (
          <p class="dialog-note" role="alert">
            This removes every rule from the new workbook.
            <br />
            <button onClick={() => void rebuild()}>Rebuild without rules</button>
            <button onClick={() => setConfirmEmpty(false)}>Keep editing</button>
          </p>
        )}
        {rebuilt && (
          <p class="rebuild-success">
            Rebuilt: {rebuilt.name}{' '}
            <button
              onClick={() =>
                void openProjectFile(plan.project, rebuilt.path).catch((reason) =>
                  setError(EngineError.from(reason).message),
                )
              }
            >
              Open
            </button>
          </p>
        )}
        <div class="dialog-actions">
          <button onClick={close}>Cancel</button>
          <button class="primary" disabled={pending || confirmEmpty} onClick={() => void rebuild()}>
            Rebuild
          </button>
        </div>
      </div>
    </div>
  );
}

function ConditionRow({
  condition,
  plan,
  onChange,
}: {
  condition: RuleCondition;
  plan: PlanCard;
  onChange: (key: keyof RuleCondition, value: string) => void;
}) {
  const column = plan.outputColumns.find((item) => `answer.${item.name}` === condition.field);
  const available = operators
    .filter(
      (operator) =>
        !['lt', 'le', 'gt', 'ge'].includes(operator) || (column && numeric(column.type)),
    )
    .filter((operator) => operator !== 'contains' || column?.type === 'string');
  const noValue = condition.op === 'is_null' || condition.op === 'not_null';
  return (
    <div class="condition-row">
      <select
        aria-label="Field"
        value={condition.field}
        onChange={(event) => onChange('field', event.currentTarget.value)}
      >
        <option value="">Field</option>
        {plan.outputColumns.map((item) => (
          <option value={`answer.${item.name}`}>answer.{item.name}</option>
        ))}
      </select>
      <select
        aria-label="Operator"
        value={condition.op}
        onChange={(event) => onChange('op', event.currentTarget.value)}
      >
        {available.map((operator) => (
          <option value={operator}>{operator}</option>
        ))}
      </select>
      {!noValue && (
        <ValueControl
          column={column}
          operator={condition.op}
          value={String(condition.value ?? '')}
          onChange={(value) => onChange('value', value)}
        />
      )}
    </div>
  );
}
function ValueControl({
  column,
  operator,
  value,
  onChange,
}: {
  column?: OutputColumn;
  operator: string;
  value: string;
  onChange: (value: string) => void;
}) {
  if (column?.type === 'boolean')
    return (
      <select
        aria-label="Value"
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
      >
        <option value="">Choose</option>
        <option value="true">Yes</option>
        <option value="false">No</option>
      </select>
    );
  if (column?.values)
    return (
      <select
        aria-label="Value"
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
      >
        <option value="">Choose</option>
        {column.values.map((item) => (
          <option value={item}>{item}</option>
        ))}
      </select>
    );
  return (
    <input
      aria-label="Value"
      type={column?.type === 'integer' ? 'number' : 'text'}
      step={column?.type === 'integer' ? '1' : undefined}
      value={value}
      onInput={(event) => onChange(event.currentTarget.value)}
      placeholder={operator === 'in' || operator === 'not_in' ? 'comma-separated' : undefined}
    />
  );
}
function numeric(type: string) {
  return type === 'integer' || type === 'number';
}
function typedCondition(condition: RuleCondition, plan: PlanCard): RuleCondition {
  const column = plan.outputColumns.find((item) => `answer.${item.name}` === condition.field);
  const values = String(condition.value ?? '')
    .split(',')
    .map((item) => item.trim());
  const convert = (item: string) =>
    column?.type === 'boolean'
      ? item === 'true'
      : numeric(column?.type ?? '')
        ? column?.type === 'integer'
          ? Number.parseInt(item, 10)
          : Number(item)
        : item;
  return {
    field: condition.field,
    op: condition.op,
    ...(condition.op === 'is_null' || condition.op === 'not_null'
      ? {}
      : {
          value:
            condition.op === 'in' || condition.op === 'not_in'
              ? values.map(convert)
              : convert(values[0] ?? ''),
        }),
  };
}
function ruleTitle(rule: Rule) {
  if (rule.kind === 'threshold_group') return rule.name;
  return `${rule.kind === 'in_scope' ? 'In scope' : rule.name}: ${rule.conditions.map((condition) => `${condition.field} ${condition.op}`).join(', ')}`;
}
