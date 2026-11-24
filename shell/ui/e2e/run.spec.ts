import { expect, test } from '@playwright/test';

test('run view shows live progress and pause and resume work', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /Webinar objections/ }).click();
  await expect(page.getByTestId('run-view')).toBeVisible();
  // The hero shows the big number and "of 8 leads read" as separate elements (as in the mockup).
  await expect(page.locator('.run-hero')).toContainText(/2\s*of 8 leads read/);
  await page.getByRole('button', { name: 'Pause', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Resume', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Resume', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Pause', exact: true })).toBeVisible();
});

test('quota pause banner shows the reset time', async ({ page }) => {
  await page.goto('/?mock=quota-pause');
  await page.getByRole('button', { name: /Webinar objections/ }).click();
  await expect(page.getByText(/Paused: ChatGPT allowance/)).toBeVisible();
  await expect(page.getByText(/allowance resets/)).toBeVisible();
});
