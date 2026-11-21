import type { AllowanceStatus, AllowanceWindow } from '../engine/types';

function windows(value: unknown): AllowanceWindow[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is AllowanceWindow =>
          typeof item === 'object' &&
          item !== null &&
          typeof (item as AllowanceWindow).name === 'string' &&
          typeof (item as AllowanceWindow).usedPercent === 'number' &&
          typeof (item as AllowanceWindow).resetsAt === 'number',
      )
    : [];
}

function longest(items: AllowanceWindow[]): AllowanceWindow | undefined {
  return items.reduce<AllowanceWindow | undefined>((longestWindow, item) => {
    if (!longestWindow) return item;
    return item.resetsAt - Date.now() > longestWindow.resetsAt - Date.now() ? item : longestWindow;
  }, undefined);
}

function findWindow(value: unknown, name: string): AllowanceWindow | undefined {
  const items = windows(value);
  return items.find((item) => item.name.toLowerCase() === name) ?? longest(items);
}

type CodexAllowance = AllowanceStatus['codex'];
type ClaudeAllowance = AllowanceStatus['claude'];

export function weeklyWindow(
  allowance: AllowanceStatus | CodexAllowance | null | undefined,
): AllowanceWindow | undefined {
  const value = allowance && 'codex' in allowance ? allowance.codex : allowance;
  return findWindow(value?.windows, 'weekly');
}

export function fiveHourWindow(
  allowance: AllowanceStatus | ClaudeAllowance | null | undefined,
): AllowanceWindow | undefined {
  const value = allowance && 'claude' in allowance ? allowance.claude : allowance;
  return findWindow(value?.windows, '5-hour');
}

export function weeklyPercent(
  allowance: AllowanceStatus | CodexAllowance | null | undefined,
): number | undefined {
  return weeklyWindow(allowance)?.usedPercent;
}

export function fiveHourPercent(
  allowance: AllowanceStatus | ClaudeAllowance | null | undefined,
): number | undefined {
  return fiveHourWindow(allowance)?.usedPercent;
}

export function resetsAtMs(window: AllowanceWindow | null | undefined): number | undefined {
  if (!window || typeof window.resetsAt !== 'number') return undefined;
  return window.resetsAt < 1e12 ? window.resetsAt * 1000 : window.resetsAt;
}
