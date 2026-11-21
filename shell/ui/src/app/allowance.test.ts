import { expect, it } from 'vitest';
import { fiveHourPercent, resetsAtMs, weeklyPercent } from './allowance';
import type { AllowanceStatus } from '../engine/types';

const allowance: AllowanceStatus = {
  codex: {
    windows: [
      { name: '5-hour', usedPercent: 7, resetsAt: 1_700_000_000 },
      { name: 'weekly', usedPercent: 42, resetsAt: 1_700_100_000 },
    ],
    stopAtPercent: 95,
  },
  claude: { checkedAt: 1_700_000_000, rateLimitInfo: {} },
};

it('weekly window is chosen regardless of order', () => {
  expect(weeklyPercent(allowance)).toBe(42);
});

it('five-hour allowance without windows is unknown', () => {
  expect(fiveHourPercent(allowance)).toBeUndefined();
});

it('resetsAt seconds render as a real date', () => {
  const window = { name: 'weekly', usedPercent: 1, resetsAt: 1_700_000_000 };
  expect(resetsAtMs(window)).toBe(1_700_000_000_000);
  expect(new Date(resetsAtMs(window)!).getFullYear()).toBeGreaterThan(2020);
});
