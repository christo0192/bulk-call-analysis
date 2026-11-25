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
type DialogRule = { definition: Rule; summary: string };
type DialogError = { message: string; client: boolean };
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
  const [rules, setRules] = useState<DialogRule[]>(
    plan.rules.flatMap((rule) =>
      rule.definition
        ? [{ definition: withoutNullValue(rule.definition), summary: rule.summary }]
        : [],
    ),
  );
  const [kind, setKind] = useState<Kind>('threshold_group');
  const [name, setName] = useState('');
  const firstNumberField = () => {
    const column = plan.outputColumns.find((item) => numeric(item.type));
    return column ? `answer.${column.name}` : '';
  };
  const [field, setField] = useState(firstNumberField);
  const [edges, setEdges] = useState('');
  const [labels, setLabels] = useState('');
  const [match, setMatch] = useState<'all' | 'any'>('all');
  const [conditions, setConditions] = useState<RuleCondition[]>([blank()]);
  const [error, setError] = useState<DialogError | null>(null);
  const [rebuilt, setRebuilt] = useState<RebuildResult | null>(null);
  const [pending, setPending] = useState(false);
  const [confirmEmpty, setConfirmEmpty] = useState(false);
  const firstJob = useRef(job);
  const hasNumberColumn = plan.outputColumns.some((column) => numeric(column.type));
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    opener.current = document.activeElement;
    const focusFirst = requestAnimationFrame(() =>
      dialog.current?.querySelector<HTMLElement>('input, select, button')?.focus(),
    );
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current();
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
  }, []);
  useEffect(() => {
    if (firstJob.current !== job) onCloseRef.current();
  }, [job]);
  useEffect(() => {
    if (
      kind === 'threshold_group' &&
      !plan.outputColumns.some((item) => `answer.${item.name}` === field && numeric(item.type))
    )
      setField(firstNumberField());
  }, [kind, plan.outputColumns, field]);
  const close = () => onClose();
  const update = (index: number, key: keyof RuleCondition, value: string) => {
    setError(null);
    setConditions((items) =>
      items.map((item, current) => {
        if (current !== index) return item;
        if (key === 'field') {
          const column = plan.outputColumns.find(
            (candidate) => `answer.${candidate.name}` === value,
          );
          const next = availableOperators(column)[0] ?? 'eq';
          return { field: value, op: next, value: '' };
        }
        return { ...item, [key]: value, ...(key === 'op' ? { value: '' } : {}) };
      }),
    );
  };
  const addRule = () => {
    setError(null);
    if (kind === 'threshold_group') {
      const rawEdges = edges.split(',').map((value) => value.trim());
      if (rawEdges.some((value) => !value || !Number.isFinite(Number(value))))
        return setError({
          message: `Edge "${rawEdges.find((value) => !value || !Number.isFinite(Number(value))) ?? ''}" is not a finite number.`,
          client: true,
        });
      const numbers = rawEdges.map(Number);
      if (numbers.some((value, index) => index > 0 && value <= numbers[index - 1]!))
        return setError({ message: 'Edges must be strictly increasing.', client: true });
      const names = labels.split(',').map((value) => value.trim());
      if (names.some((value) => !value) || names.length !== numbers.length + 1)
        return setError({
          message: 'Labels must contain one more non-empty item than edges.',
          client: true,
        });
      if (new Set(names).size !== names.length)
        return setError({ message: 'Labels must be unique.', client: true });
      setRules((items) => [
        ...items,
        {
          definition: {
            kind,
            name:
              name ||
              nextName(
                'band',
                items.map((item) => item.definition),
              ),
            field,
            edges: numbers,
            labels: names,
          },
          summary:
            name ||
            nextName(
              'band',
              items.map((item) => item.definition),
            ),
        },
      ]);
    } else {
      // The engine allows one in-scope rule per job; a second one is refused at rebuild time.
      if (kind === 'in_scope' && rules.some((item) => item.definition.kind === 'in_scope'))
        return setError({
          message: 'Only one in-scope rule is allowed. Add a review flag instead.',
          client: true,
        });
      if (conditions.some((item) => !item.field || !item.op))
        return setError({ message: 'Enter a value', client: true });
      const validation = conditions.flatMap((condition) => validateCondition(condition, plan));
      if (validation.length) return setError({ message: validation[0]!, client: true });
      const definition = {
        kind,
        ...(kind === 'review_flag'
          ? {
              name:
                name ||
                nextName(
                  'review flag',
                  rules.map((item) => item.definition),
                ),
            }
          : {}),
        match,
        conditions: conditions.map((condition) => typedCondition(condition, plan)),
      } as Rule;
      setRules((items) => [...items, { definition, summary: ruleTitle(definition) }]);
    }
    setName('');
    setEdges('');
    setLabels('');
    setConditions([blank()]);
    setConfirmEmpty(false);
  };
  const rebuild = async () => {
    if (!rules.length && !confirmEmpty) {
      setConfirmEmpty(true);
      return;
    }
    setPending(true);
    setError(null);
    try {
      const result = await bridge.call('job/rebuildRules', {
        job,
        rules: rules.map((item) => item.definition),
      });
      setRebuilt(result);
      setConfirmEmpty(false);
      onRebuilt?.();
    } catch (reason) {
      const parsed = EngineError.from(reason);
      const detail = (parsed.data as { detail?: unknown } | null)?.detail;
      setError({
        message:
          parsed.reason === 'invalid_rules' && typeof detail === 'string' ? detail : parsed.message,
        client: false,
      });
    } finally {
      setPending(false);
    }
  };
  const currentInScope = rules.some((item) => item.definition.kind === 'in_scope');
  // Once an in-scope rule exists, leave the in-scope form so it can't be filled in again.
  useEffect(() => {
    if (currentInScope && kind === 'in_scope') setKind('review_flag');
  }, [currentInScope, kind]);
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
        {rules.map((item, index) => (
          <div class="rule-row" key={`${item.definition.kind}-${index}`} title={item.summary}>
            <span>{item.summary}</span>
            <button
              onClick={() => {
                setConfirmEmpty(false);
                setError(null);
                setRules((items) => items.filter((_, item) => item !== index));
              }}
            >
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
            onClick={() => {
              setError(null);
              setKind('in_scope');
            }}
          >
            In scope
          </button>
          <button
            class={kind === 'threshold_group' ? 'selected' : ''}
            onClick={() => {
              setError(null);
              setKind('threshold_group');
            }}
          >
            Bands
          </button>
          <button
            class={kind === 'review_flag' ? 'selected' : ''}
            onClick={() => {
              setError(null);
              setKind('review_flag');
            }}
          >
            Review flag
          </button>
        </div>
        {kind !== 'in_scope' && (
          <label>
            Name
            <input
              value={name}
              onInput={(event) => {
                setError(null);
                setName(event.currentTarget.value);
              }}
            />
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
                  <input
                    value={edges}
                    onInput={(event) => {
                      setError(null);
                      setEdges(event.currentTarget.value);
                    }}
                  />
                </label>
                <label>
                  Labels
                  <input
                    value={labels}
                    onInput={(event) => {
                      setError(null);
                      setLabels(event.currentTarget.value);
                    }}
                  />
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
                onChange={(event) => {
                  setError(null);
                  setMatch(event.currentTarget.value as 'all' | 'any');
                }}
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
            <button
              onClick={() => {
                setError(null);
                setConditions((items) => [...items, blank()]);
              }}
            >
              Add condition
            </button>
          </>
        )}
        <button
          disabled={error?.client === true || (kind === 'threshold_group' && !hasNumberColumn)}
          onClick={addRule}
        >
          Add rule
        </button>
        {error && (
          <p class="dialog-error" role="alert">
            {error.message}
          </p>
        )}
        {confirmEmpty && (
          <p class="dialog-note" role="alert">
            This removes every rule from the new workbook.
            <br />
            <button disabled={pending} onClick={() => void rebuild()}>
              Rebuild without rules
            </button>
            <button onClick={() => setConfirmEmpty(false)}>Keep editing</button>
          </p>
        )}
        {rebuilt && (
          <p class="rebuild-success">
            Rebuilt: {rebuilt.name}{' '}
            <button
              onClick={() =>
                void openProjectFile(plan.project, rebuilt.path).catch((reason) =>
                  setError({ message: EngineError.from(reason).message, client: false }),
                )
              }
            >
              Open
            </button>
          </p>
        )}
        <div class="dialog-actions">
          <button onClick={close}>Cancel</button>
          <button
            class="primary"
            disabled={pending || confirmEmpty || error?.client === true}
            onClick={() => void rebuild()}
          >
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
  const available = availableOperators(column);
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
  if (column?.values) {
    // A list operator picks several options; they travel joined by OPTION_SEPARATOR so an option
    // with a comma or spaces stays exactly as the engine defines it. Options can be numbers (an
    // integer enum), so they are compared as text here and converted back by the column type.
    const list = isList(operator);
    const options = enumOptions(column);
    const chosen = list ? value.split(OPTION_SEPARATOR).filter(Boolean) : [value];
    return (
      <select
        aria-label="Value"
        multiple={list}
        size={list ? Math.min(Math.max(options.length, 2), 6) : undefined}
        title={list ? 'Hold Ctrl (or ⌘ on a Mac) to pick several' : undefined}
        {...(list ? {} : { value })}
        onChange={(event) => {
          const target = event.currentTarget;
          onChange(
            list
              ? Array.from(target.selectedOptions, (option) => option.value)
                  .filter(Boolean)
                  .join(OPTION_SEPARATOR)
              : target.value,
          );
        }}
      >
        {!list && <option value="">Choose</option>}
        {options.map((item) => (
          <option
            value={item}
            key={item}
            title={item}
            {...(list ? { selected: chosen.includes(item) } : {})}
          >
            {item}
          </option>
        ))}
      </select>
    );
  }
  // Always a text box: a number box drops the commas of a list ("1, 2" becomes 12) and
  // accepts forms the engine reads differently. The values are checked on Add rule.
  const numberColumn = numeric(column?.type ?? '');
  return (
    <input
      aria-label="Value"
      type="text"
      inputMode={numberColumn ? (column?.type === 'integer' ? 'numeric' : 'decimal') : undefined}
      value={value}
      onInput={(event) => onChange(event.currentTarget.value)}
      placeholder={
        operator === 'in' || operator === 'not_in'
          ? numberColumn
            ? 'numbers, comma-separated'
            : 'comma-separated'
          : undefined
      }
    />
  );
}
function numeric(type: string) {
  return type === 'integer' || type === 'number';
}
function availableOperators(column?: OutputColumn) {
  return operators
    .filter(
      (operator) =>
        !['lt', 'le', 'gt', 'ge'].includes(operator) || (column && numeric(column.type)),
    )
    .filter((operator) => operator !== 'contains' || column?.type === 'string');
}
const INTEGER = /^-?\d+$/;
// Plain decimals only: no hex, no exponent, no thousands separators.
const DECIMAL = /^-?(?:\d+(?:\.\d+)?|\.\d+)$/;
const isList = (op: string) => op === 'in' || op === 'not_in';
/**
 * An enum column's options as text, in the engine's order. Empty and null options are left out:
 * "is empty" covers them, and an empty option would look like "nothing chosen".
 */
function enumOptions(column: OutputColumn): string[] {
  return (column.values ?? [])
    .filter((option) => option !== null && option !== '')
    .map((option) => String(option));
}
/** Joins the options chosen for an enum list; never part of an option the engine sends. */
const OPTION_SEPARATOR = '\u001f';
/**
 * The typed text, split into items only for the list operators. A comma inside a single value is
 * part of that value ("too expensive, will think"); text columns keep their spaces. Options picked
 * from an enum are sent exactly as the engine lists them, never split or trimmed.
 */
function conditionItems(condition: RuleCondition, column?: OutputColumn): string[] {
  const raw = String(condition.value ?? '');
  if (column?.values)
    return isList(condition.op) ? raw.split(OPTION_SEPARATOR).filter((item) => item !== '') : [raw];
  const text = column?.type === 'string' || !column;
  if (!isList(condition.op)) return [text ? raw : raw.trim()];
  return raw.split(',').map((item) => item.trim());
}
function validateCondition(condition: RuleCondition, plan: PlanCard): string[] {
  if (condition.op === 'is_null' || condition.op === 'not_null') return [];
  const column = plan.outputColumns.find((item) => `answer.${item.name}` === condition.field);
  const values = conditionItems(condition, column);
  if (column?.type === 'boolean' && values.some((value) => value !== 'true' && value !== 'false'))
    return ['Choose Yes or No'];
  if (column?.values) {
    // Enum options are sent exactly as listed; only check that one was picked.
    const options = enumOptions(column);
    if (!values.length || values.some((value) => !options.includes(value)))
      return ['Choose at least one option'];
    return [];
  }
  if (values.some((value) => !value.trim())) return ['Enter a value'];
  if (column?.type === 'integer' && values.some((value) => !INTEGER.test(value)))
    return ['Whole numbers only'];
  if (column?.type === 'number' && values.some((value) => !DECIMAL.test(value)))
    return ['Enter a number, like 12 or 12.5'];
  return [];
}
function typedCondition(condition: RuleCondition, plan: PlanCard): RuleCondition {
  const column = plan.outputColumns.find((item) => `answer.${item.name}` === condition.field);
  const values = conditionItems(condition, column);
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
          value: isList(condition.op) ? values.map(convert) : convert(values[0] ?? ''),
        }),
  };
}
function nextName(base: string, rules: Rule[]) {
  const names = new Set(rules.map((rule) => ('name' in rule ? rule.name : undefined)));
  let index = 1;
  while (names.has(`${base} ${index}`)) index += 1;
  return `${base} ${index}`;
}
function withoutNullValue(rule: Rule): Rule {
  if (!('conditions' in rule)) return rule;
  return {
    ...rule,
    conditions: rule.conditions.map((condition) =>
      condition.op === 'is_null' || condition.op === 'not_null'
        ? { field: condition.field, op: condition.op }
        : condition,
    ),
  } as Rule;
}
const OPERATOR_WORDS: Record<string, string> = {
  eq: 'is',
  ne: 'is not',
  in: 'is one of',
  not_in: 'is none of',
  lt: '<',
  le: '≤',
  gt: '>',
  ge: '≥',
  is_null: 'is empty',
  not_null: 'is not empty',
  contains: 'contains',
};
function conditionText(condition: RuleCondition) {
  const field = condition.field.replace(/^answer\./, '');
  const words = OPERATOR_WORDS[condition.op] ?? condition.op;
  if (condition.op === 'is_null' || condition.op === 'not_null') return `${field} ${words}`;
  const value = Array.isArray(condition.value)
    ? condition.value.map((item) => JSON.stringify(item)).join(', ')
    : JSON.stringify(condition.value);
  return `${field} ${words} ${value}`;
}
/** A new rule's title shows its values, so a mistyped value is visible before Rebuild. */
function ruleTitle(rule: Rule) {
  if (rule.kind === 'threshold_group')
    return `${rule.name}: ${rule.field.replace(/^answer\./, '')} bands at ${rule.edges.join(', ')} (${rule.labels.join(', ')})`;
  const joiner = rule.match === 'any' ? ' or ' : ' and ';
  return `${rule.kind === 'in_scope' ? 'In scope' : rule.name}: ${rule.conditions.map(conditionText).join(joiner)}`;
}
