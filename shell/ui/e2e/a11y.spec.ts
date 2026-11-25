import { expect, test, type Locator, type Page } from '@playwright/test';

/**
 * Press Tab (or Shift+Tab) until `target` has focus. Every stop on the way must show a focus
 * ring; the stops without one are collected in `noRing`.
 */
async function tabTo(page: Page, target: Locator, noRing: string[], key = 'Tab') {
  await target.waitFor();
  for (let presses = 0; presses < 60; presses++) {
    if (await target.evaluate((element) => element === document.activeElement)) return;
    await page.keyboard.press(key);
    const missing = await page.evaluate(() => {
      const element = document.activeElement;
      if (!element || element === document.body) return null;
      const ring = (node: Element) => {
        const style = getComputedStyle(node);
        return style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0;
      };
      // The composer draws one ring around the box and its buttons.
      const composer = element.closest('.composer');
      if (ring(element) || (composer && ring(composer))) return null;
      return `${element.tagName.toLowerCase()} "${(element.textContent ?? '').trim().slice(0, 40)}"`;
    });
    if (missing) noRing.push(missing);
  }
  throw new Error(`${key} never reached ${target.toString()}`);
}

test('keyboard only: create a project, approve a plan and open settings', async ({ page }) => {
  const noRing: string[] = [];
  await page.goto('/?mock=empty');
  await expect(page.getByRole('heading', { name: 'No projects yet' })).toBeVisible();

  // Create a project: the first Tab stop is "+ New project"; the title box takes focus.
  await tabTo(page, page.getByRole('button', { name: '+ New project' }), noRing);
  await page.keyboard.press('Enter');
  await expect(page.getByLabel('Project title')).toBeFocused();
  await page.keyboard.type('Synthetic keyboard review');
  await page.keyboard.press('Enter');
  await expect(page.locator('main header h1')).toHaveText('Synthetic keyboard review');
  // Focus is not lost when the form closes: it moves to the new project.
  await expect(page.getByRole('button', { name: /Synthetic keyboard review/ })).toBeFocused();

  // Ask Claude: Shift+Enter adds a line, Enter sends, and focus stays in the composer.
  const composer = page.getByPlaceholder('Ask Claude about this job…');
  await tabTo(page, composer, noRing);
  await page.keyboard.type('What do callers ask about?');
  await page.keyboard.press('Shift+Enter');
  await page.keyboard.type('Start with a small sample.');
  await expect(composer).toHaveValue('What do callers ask about?\nStart with a small sample.');
  await page.keyboard.press('Enter');
  await expect(page.locator('.chat-user')).toHaveText(
    'What do callers ask about?\nStart with a small sample.',
  );
  await expect(composer).toHaveValue('');
  await expect(composer).toBeFocused();

  // Claude prepares the plan; approve it from the keyboard.
  const approve = page.getByRole('button', { name: 'Approve plan' });
  await expect(approve).toBeVisible();
  await tabTo(page, approve, noRing, 'Shift+Tab');
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { name: /Sample · 20 leads/ })).toBeVisible({
    timeout: 5000,
  });
  // The approved card went away with its view; focus waits on the project heading.
  await expect(page.locator('main header h1')).toBeFocused();

  // Open settings with Space this time; focus starts on the Settings heading.
  await tabTo(page, page.getByRole('button', { name: 'Settings' }), noRing, 'Shift+Tab');
  await page.keyboard.press('Space');
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeFocused();
  // And back again: focus returns to the Settings button.
  await tabTo(page, page.getByRole('button', { name: 'Back' }), noRing);
  await page.keyboard.press('Enter');
  await expect(page.getByRole('button', { name: 'Settings' })).toBeFocused();

  expect(noRing, 'Tab stops without a visible focus ring').toEqual([]);
});

test('keyboard only: dialogs keep focus inside, close on Escape and give focus back', async ({
  page,
}) => {
  const noRing: string[] = [];
  await page.goto('/');
  await page.getByRole('button', { name: /Shipping questions/ }).click();
  const rebuild = page.getByRole('button', { name: 'Rebuild with new rules' });
  await tabTo(page, rebuild, noRing);
  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  const close = dialog.getByRole('button', { name: 'Close' });
  await expect(close).toBeFocused();
  // Thirty Tabs never leave the dialog, and they come round to the first button again.
  let cameRound = false;
  for (let presses = 0; presses < 30; presses++) {
    await page.keyboard.press('Tab');
    expect(await dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true);
    cameRound ||= await close.evaluate((element) => element === document.activeElement);
  }
  expect(cameRound, 'Tab wraps from the last control to the first').toBe(true);
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(rebuild).toBeFocused();

  await tabTo(page, page.getByRole('button', { name: 'Settings' }), noRing, 'Shift+Tab');
  await page.keyboard.press('Enter');
  const change = page.getByRole('button', { name: 'Change Claude model' });
  await tabTo(page, change, noRing);
  await page.keyboard.press('Enter');
  const modelDialog = page.getByRole('dialog', { name: 'Change the Claude model' });
  const newModel = modelDialog.getByLabel('New model');
  await expect(newModel).toBeFocused();
  // Shift+Tab goes backwards round the three controls and never leaves.
  let backRound = false;
  for (let presses = 0; presses < 6; presses++) {
    await page.keyboard.press('Shift+Tab');
    expect(await modelDialog.evaluate((element) => element.contains(document.activeElement))).toBe(
      true,
    );
    backRound ||= await newModel.evaluate((element) => element === document.activeElement);
  }
  expect(backRound, 'Shift+Tab wraps from the first control to the last').toBe(true);
  await page.keyboard.press('Escape');
  await expect(modelDialog).toBeHidden();
  await expect(change).toBeFocused();
  expect(noRing, 'Tab stops without a visible focus ring').toEqual([]);
});

test('reduced motion: nothing animates on the run view and the spinner shows …', async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await page.getByRole('button', { name: /Webinar objections/ }).click();
  const spinner = page.locator('.step-icon.active .spinner');
  await expect(spinner).toBeVisible();
  // Let the mock's progress events arrive, so the progress bar has had a chance to move.
  await page.waitForTimeout(2500);
  const running = await page.evaluate(() =>
    document
      .getAnimations()
      .filter((animation) => animation.playState === 'running')
      .map((animation) => {
        const target = (animation.effect as KeyframeEffect | null)?.target;
        return `${animation.constructor.name} on ${target?.className ?? 'unknown'}`;
      }),
  );
  expect(running).toEqual([]);
  expect(await spinner.evaluate((element) => getComputedStyle(element, '::before').content)).toBe(
    '"…"',
  );
});

test('with motion allowed, the active step spinner turns', async ({ page }) => {
  // The reduced-motion test above would pass on a page with no animations at all; this shows
  // the same check does see the spinner when motion is allowed.
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.goto('/');
  await page.getByRole('button', { name: /Webinar objections/ }).click();
  await expect(page.locator('.step-icon.active .spinner')).toBeVisible();
  const running = await page.evaluate(
    () => document.getAnimations().filter((animation) => animation.playState === 'running').length,
  );
  expect(running).toBeGreaterThan(0);
});
