import { expect, test } from '@playwright/test';

test('done view opens results and rebuilds with a new bands rule', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /Shipping questions/ }).click();
  await expect(page.getByTestId('done-view')).toBeVisible();
  await page.getByRole('button', { name: 'Open results' }).click();
  await page.getByRole('button', { name: 'Rebuild with new rules' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Bands' }).click();
  await dialog.getByLabel('Edges').fill('10, 20');
  await dialog.getByLabel('Labels').fill('low, medium, high');
  await dialog.getByRole('button', { name: 'Add rule' }).click();
  await dialog.getByRole('button', { name: 'Rebuild' }).click();
  // The dialog shows the file name only; it opens by the project-relative path.
  await expect(dialog).toContainText('Rebuilt: results.v2.rebuild1.xlsx');
  const rebuildCalls = await page.evaluate(async () => {
    const { mockBridge } = await import('/src/engine/mockBridge.ts');
    return mockBridge.calls.filter((call) => call.method === 'job/rebuildRules');
  });
  expect(rebuildCalls).toHaveLength(1);
  expect(rebuildCalls[0]?.params).toMatchObject({
    job: 'synthetic-job-5',
    rules: expect.arrayContaining([
      {
        kind: 'threshold_group',
        name: 'band 1',
        field: 'answer.discount_percent',
        edges: [10, 20],
        labels: ['low', 'medium', 'high'],
      },
    ]),
  });
  await dialog.getByRole('button', { name: 'Open' }).click();
});

test('typed number lists keep every item in the real browser', async ({ page }) => {
  // A number box in Chromium drops the commas of "1.5, 2" (it became 152 before this fix).
  await page.goto('/');
  await page.getByRole('button', { name: /Shipping questions/ }).click();
  await page.getByRole('button', { name: 'Rebuild with new rules' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Review flag' }).click();
  await dialog.getByLabel('Field').selectOption('answer.discount_percent');
  await dialog.getByLabel('Operator').selectOption('in');
  await dialog.getByLabel('Value').pressSequentially('1.5, 2');
  await dialog.getByRole('button', { name: 'Add rule' }).click();
  await expect(dialog).toContainText('discount_percent is one of 1.5, 2');
  await dialog.getByRole('button', { name: 'Rebuild' }).click();
  await expect(dialog).toContainText('Rebuilt:');
  const rebuildCalls = await page.evaluate(async () => {
    const { mockBridge } = await import('/src/engine/mockBridge.ts');
    return mockBridge.calls.filter((call) => call.method === 'job/rebuildRules');
  });
  const rules = (rebuildCalls[0]?.params as { rules: { conditions?: unknown[] }[] }).rules;
  expect(rules.at(-1)).toMatchObject({
    kind: 'review_flag',
    conditions: [{ field: 'answer.discount_percent', op: 'in', value: [1.5, 2] }],
  });
});

test('an integer list typed in the real browser keeps every item', async ({ page }) => {
  // Chromium's number box drops the commas of "1, 2" (it becomes 12); the value box is text.
  await page.goto('/');
  await page.getByRole('button', { name: /Shipping questions/ }).click();
  await page.getByRole('button', { name: 'Rebuild with new rules' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Review flag' }).click();
  await dialog.getByLabel('Field').selectOption('answer.call_count');
  await dialog.getByLabel('Operator').selectOption('in');
  await dialog.getByLabel('Value').pressSequentially('1, 2');
  await dialog.getByRole('button', { name: 'Add rule' }).click();
  await dialog.getByRole('button', { name: 'Rebuild' }).click();
  await expect(dialog).toContainText('Rebuilt:');
  const rebuildCalls = await page.evaluate(async () => {
    const { mockBridge } = await import('/src/engine/mockBridge.ts');
    return mockBridge.calls.filter((call) => call.method === 'job/rebuildRules');
  });
  const rules = (rebuildCalls[0]?.params as { rules: unknown[] }).rules;
  expect(rules.at(-1)).toMatchObject({
    kind: 'review_flag',
    conditions: [{ field: 'answer.call_count', op: 'in', value: [1, 2] }],
  });
});
