import { expect, test } from '@playwright/test';

test('chat: send a message, see streamed text, a tool line that completes, and the reply once', async ({
  page,
}) => {
  await page.goto('/');
  await page.getByRole('button', { name: /Discount questions/ }).click();
  const chat = page.getByRole('region', { name: 'Claude chat' });
  await chat.getByRole('textbox').fill('What should we learn?');
  await chat.getByRole('button', { name: 'Send' }).click();
  await expect(chat.getByText('Ran a read-only query')).toBeVisible();
  await expect(chat.locator('.tool-done').first()).toBeVisible();
  const reply = chat.getByText(/I checked the read-only data/);
  await expect(reply).toHaveCount(1);
});

test('chat: two turns keep order and no duplicates', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /Discount questions/ }).click();
  const chat = page.getByRole('region', { name: 'Claude chat' });
  const box = chat.getByRole('textbox');
  await box.fill('First question');
  await chat.getByRole('button', { name: 'Send' }).click();
  await expect(chat.getByText(/I checked the read-only data/)).toHaveCount(1);
  await box.fill('Second question');
  await chat.getByRole('button', { name: 'Send' }).click();
  await expect(chat.getByText(/I checked the read-only data/)).toHaveCount(2);
  await expect(chat.locator('.chat-user')).toHaveText(['First question', 'Second question']);
});

test('cards: approve plan → sample card → approve sample → start card → start full run', async ({
  page,
}) => {
  await page.goto('/');
  await page.getByRole('button', { name: /Discount questions/ }).click();
  await page.getByRole('button', { name: 'Approve plan' }).click();
  await expect(page.getByRole('heading', { name: /Sample/ })).toBeVisible({ timeout: 5000 });
  await page.getByRole('button', { name: 'Approve sample' }).click();
  await expect(page.getByRole('heading', { name: 'Start the full run?' })).toBeVisible();
  await page.getByRole('button', { name: 'Start full run' }).click();
  await expect(page.getByTestId('run-view')).toBeVisible();
});

test('primary buttons are visible', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /Discount questions/ }).click();
  for (const name of ['Approve plan', 'Send']) {
    const background = await page
      .getByRole('button', { name })
      .evaluate((button) => getComputedStyle(button).backgroundColor);
    expect(background).not.toMatch(/transparent|rgba?\(255, 255, 255/);
  }
});

test('sidebar survives a claude allowance without windows', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('Claude 5-hour')).toBeVisible();
  await expect(page.getByText('unknown')).toBeVisible();
});
