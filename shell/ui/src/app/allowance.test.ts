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

it("five-hour percent reads the engine's 5h window", () => {
  // The five-hour figure is Claude's (the sidebar's "Claude 5-hour" line), so the window
  // must sit under `claude`, not under the Codex windows above.
  expect(
    fiveHourPercent({
      codex: null,
      claude: {
        checkedAt: 1_700_000_000,
        rateLimitInfo: {},
        windows: [{ name: '5h', usedPercent: 7, resetsAt: 1_700_000_000 }],
      },
    } as AllowanceStatus),
  ).toBe(7);
});

it('five-hour allowance without windows is unknown', () => {
  expect(
    fiveHourPercent({
      codex: { windows: [], stopAtPercent: 95 },
      claude: null,
    }),
  ).toBeUndefined();
});

it('resetsAt seconds render as a real date', () => {
  const window = { name: 'weekly', usedPercent: 1, resetsAt: 1_700_000_000 };
  expect(resetsAtMs(window)).toBe(1_700_000_000_000);
  expect(new Date(resetsAtMs(window)!).getFullYear()).toBeGreaterThan(2020);
});
