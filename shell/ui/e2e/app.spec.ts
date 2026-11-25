import { test, expect } from '@playwright/test';

test('loads without console errors', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  await page.goto('/');
  await expect(page.getByText('Bulk Call Analysis')).toBeVisible();
  expect(errors).toEqual([]);
});

test('projects show their phase views', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /Discount questions/ }).click();
  await expect(page.getByTestId('setup-view')).toBeVisible();
  await page.getByRole('button', { name: /Certificate mentions/ }).click();
  await expect(page.getByTestId('sample-view')).toBeVisible();
  await page.getByRole('button', { name: /Webinar objections/ }).click();
  await expect(page.getByTestId('run-view')).toBeVisible();
});

test('rail and drawer hide and show', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('Files')).toBeVisible();
  await page.getByRole('button', { name: /Webinar objections/ }).click();
  await page.getByRole('button', { name: 'Hide chat' }).click();
  await expect(page.getByRole('button', { name: 'Show chat' })).toBeVisible();
  await page.getByRole('button', { name: 'Show chat' }).click();
  await expect(page.locator('.drawer').getByRole('textbox')).toBeVisible();
});

test('new project flow works', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /New project/ }).click();
  await page.getByLabel('Project title').fill('Synthetic review');
  await page.getByRole('button', { name: 'Create' }).click();
  await expect(page.getByRole('heading', { name: 'Synthetic review' })).toBeVisible();
  await expect(page.locator('main header h1')).toHaveText('Synthetic review');
});

test('first run: sign in to Claude, confirm the two manual items, continue to the main frame', async ({
  page,
}) => {
  await page.goto('/?mock=firstrun');
  await expect(page.getByRole('heading', { name: 'Set up this computer' })).toBeVisible();
  await page
    .getByText('Sign in to Claude')
    .locator('..')
    .locator('..')
    .getByRole('button', { name: 'Sign in' })
    .click();
  const claudeRow = page.getByText('Sign in to Claude').locator('..').locator('..');
  const checkAgain = claudeRow.getByRole('button', { name: 'Check again' });
  await expect(checkAgain).toBeEnabled();
  await checkAgain.click();
  await expect(checkAgain).toBeEnabled();
  await checkAgain.click();
  await page.getByRole('button', { name: "I've checked" }).first().click();
  await page.getByRole('button', { name: "I've checked" }).first().click();
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.getByRole('heading', { name: 'Set up this computer' })).not.toBeVisible();
  await expect(page.locator('main header h1')).toBeVisible();
});

test('no auth: auth/status failing still shows the checklist and the projects load', async ({
  page,
}) => {
  await page.goto('/?mock=noauth');
  await expect(page.getByRole('heading', { name: 'Set up this computer' })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Projects' })).toBeVisible();
});

test('settings: change the worker count and see Saved', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /Settings/ }).click();
  await page.getByLabel('Worker count').selectOption('3');
  await expect(page.getByText('Saved')).toBeVisible();
});
