import { expect, test, type Page } from '@playwright/test';

/**
 * Every main screen at three window sizes: no clipped or truncated text, nothing off the window,
 * and no console errors. Screenshots go to test-results/screens/ for a person to look at.
 */
const SIZES = [
  { width: 1280, height: 720 },
  { width: 1366, height: 768 },
  { width: 1920, height: 1080 },
];

type Screen = { name: string; open: (page: Page) => Promise<void> };

const project = (title: RegExp) => async (page: Page) => {
  await page.goto('/');
  await page.getByRole('button', { name: title }).click();
};

const SCREENS: Screen[] = [
  {
    name: 'first-run checklist',
    open: async (page) => {
      await page.goto('/?mock=firstrun');
      await expect(page.getByText('Set up this computer')).toBeVisible();
    },
  },
  { name: 'setup with the plan card', open: project(/Discount questions/) },
  { name: 'sample card', open: project(/Certificate mentions/) },
  {
    name: 'run view',
    open: async (page) => {
      await project(/Webinar objections/)(page);
      await expect(page.getByTestId('run-view')).toBeVisible();
    },
  },
  {
    name: 'run view with the chat drawer hidden',
    open: async (page) => {
      await project(/Webinar objections/)(page);
      await expect(page.getByTestId('run-view')).toBeVisible();
      // The drawer starts open in the run phase; hide it to the tab.
      await page.getByRole('button', { name: 'Hide ›' }).click();
      await expect(page.getByRole('button', { name: '‹ Chat' })).toBeVisible();
    },
  },
  { name: 'paused run', open: project(/Renewal concerns/) },
  {
    name: 'quota pause banner',
    open: async (page) => {
      await page.goto('/?mock=quota-pause');
      await page.getByRole('button', { name: /Webinar objections/ }).click();
      await expect(page.getByText(/Paused: ChatGPT allowance/)).toBeVisible();
    },
  },
  {
    name: 'done view',
    open: async (page) => {
      await project(/Shipping questions/)(page);
      await expect(page.getByTestId('done-view')).toBeVisible();
    },
  },
  {
    name: 'rebuild dialog',
    open: async (page) => {
      await project(/Shipping questions/)(page);
      await page.getByRole('button', { name: 'Rebuild with new rules' }).click();
      await expect(page.getByRole('dialog')).toBeVisible();
    },
  },
  {
    name: 'settings',
    open: async (page) => {
      await page.goto('/');
      await page.getByRole('button', { name: 'Settings' }).click();
    },
  },
];

/** Elements with their own visible text that are clipped, truncated or off the window. */
async function layoutProblems(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const problems: string[] = [];
    const viewport = document.documentElement.clientWidth;
    const describe = (element: Element) => {
      const tag = element.tagName.toLowerCase();
      const cls = element.getAttribute('class');
      const text = (element.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 60);
      return `${tag}${cls ? `.${cls.split(/\s+/).join('.')}` : ''} "${text}"`;
    };
    for (const element of Array.from(document.querySelectorAll('body *'))) {
      const style = getComputedStyle(element);
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      const ownText = Array.from(element.childNodes).some(
        (node) => node.nodeType === Node.TEXT_NODE && (node.textContent ?? '').trim(),
      );
      if (!ownText) continue;
      const box = element.getBoundingClientRect();
      // Screen-reader-only labels are hidden on purpose (a 1px clipped box).
      if (box.width <= 1 || box.height <= 1) continue;
      const hidesX = ['hidden', 'clip'].includes(style.overflowX);
      const hidesY = ['hidden', 'clip'].includes(style.overflowY);
      if (hidesX && element.scrollWidth > element.clientWidth + 1)
        problems.push(`clipped sideways: ${describe(element)}`);
      if (hidesY && element.scrollHeight > element.clientHeight + 1)
        problems.push(`clipped vertically: ${describe(element)}`);
      if (style.textOverflow === 'ellipsis') problems.push(`ellipsis: ${describe(element)}`);
      const clamp = style.getPropertyValue('-webkit-line-clamp');
      if (clamp && clamp !== 'none') problems.push(`line clamp: ${describe(element)}`);
      if (box.right > viewport + 1 || box.left < -1)
        problems.push(`off the window: ${describe(element)}`);
    }
    return problems;
  });
}

for (const size of SIZES) {
  test.describe(`${size.width}x${size.height}`, () => {
    test.use({ viewport: size });
    for (const screen of SCREENS) {
      test(`${screen.name} has no clipped text or console errors`, async ({ page }) => {
        const errors: string[] = [];
        page.on('console', (message) => {
          if (message.type() === 'error') errors.push(message.text());
        });
        page.on('pageerror', (error) => errors.push(String(error)));
        await screen.open(page);
        // Let the mock's first answers and any banners settle.
        await page.waitForTimeout(400);
        await page.screenshot({
          path: `test-results/screens/${size.width}x${size.height}-${screen.name.replace(/\s+/g, '-')}.png`,
          fullPage: true,
        });
        expect(await layoutProblems(page), 'layout problems').toEqual([]);
        expect(errors, 'console errors').toEqual([]);
      });
    }
  });
}
