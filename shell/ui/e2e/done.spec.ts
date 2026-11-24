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
  await expect(dialog).toContainText('output/results.v2.rebuild1.xlsx');
  await dialog.getByRole('button', { name: 'Open' }).click();
});
