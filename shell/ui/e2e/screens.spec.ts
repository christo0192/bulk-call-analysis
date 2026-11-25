import { expect, test, type Page } from '@playwright/test';

/**
 * Every main screen at four window sizes: no clipped or truncated text, nothing off the window,
 * and no console errors. Screenshots go to test-results/screens/ for a person to look at.
 */
const SIZES = [
  { width: 1280, height: 720 },
  { width: 1366, height: 768 },
  { width: 1920, height: 1080 },
  { width: 2560, height: 1440 },
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
  {
    name: 'setup with the plan card',
    open: async (page) => {
      await project(/Discount questions/)(page);
      await expect(page.getByRole('button', { name: 'Approve plan' })).toBeVisible();
    },
  },
  {
    name: 'sample card',
    open: async (page) => {
      await project(/Certificate mentions/)(page);
      await expect(page.getByRole('button', { name: 'Approve sample' })).toBeVisible();
    },
  },
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
      await page.getByRole('button', { name: 'Hide chat' }).click();
      await expect(page.getByRole('button', { name: 'Show chat' })).toBeVisible();
    },
  },
  {
    name: 'paused run',
    open: async (page) => {
      await project(/Renewal concerns/)(page);
      // Paused by the person: the Paused pill and Resume, and no banner.
      await expect(page.locator('main header .pill')).toHaveText('Paused');
      await expect(page.getByRole('button', { name: 'Resume', exact: true })).toBeVisible();
      await expect(page.locator('.run-hero')).toBeVisible();
    },
  },
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
      await expect(page.getByRole('heading', { name: 'Every lead accounted for' })).toBeVisible();
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
      await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
      await expect(page.getByText('Loading settings…')).toBeHidden();
    },
  },
  {
    name: 'model change dialog',
    open: async (page) => {
      await page.goto('/');
      await page.getByRole('button', { name: 'Settings' }).click();
      await page.getByRole('button', { name: 'Change Claude model' }).click();
      await expect(page.getByRole('dialog')).toBeVisible();
    },
  },
  {
    name: 'empty state',
    open: async (page) => {
      await page.goto('/?mock=empty');
      await expect(page.getByRole('heading', { name: 'No projects yet' })).toBeVisible();
    },
  },
  {
    name: 'start card',
    open: async (page) => {
      await project(/Certificate mentions/)(page);
      await page.getByRole('button', { name: 'Approve sample' }).click();
      await expect(page.getByRole('heading', { name: 'Start the full run?' })).toBeVisible();
      await expect(page.getByText(/Leads to read/)).toBeVisible();
    },
  },
  {
    name: 'engine status banner',
    open: async (page) => {
      await page.goto('/?mock=engine-restarting');
      await expect(page.locator('.banner')).toHaveText(
        'The engine stopped unexpectedly. Restarting…',
      );
      await expect(page.locator('main header h1')).toBeVisible();
    },
  },
  // Every other banner the run view can show (the quota pause is above).
  ...(
    [
      ['credit-pause', 'Paused to avoid paid credits'],
      ['spend-control-pause', 'Paused: spending control'],
      ['auth-pause', 'Paused: sign-in needed'],
      ['model-unavailable-pause', 'Paused: model unavailable'],
      ['error-storm-pause', 'Paused: too many errors'],
      ['network-pause', 'Paused: network unavailable'],
      ['sleep-pause', 'Paused while the computer slept'],
      ['power-pause', 'Paused: computer is on battery'],
      ['stalled', 'Stopped: needs attention'],
      ['failed', 'Run failed'],
      ['cancelled', 'Run cancelled'],
    ] as const
  ).map(([mode, title]): Screen => ({
    name: `${mode} banner`,
    open: async (page) => {
      await page.goto(`/?mock=${mode}`);
      await page.getByRole('button', { name: /Webinar objections/ }).click();
      await expect(page.locator('.run-banner strong')).toHaveText(title);
    },
  })),
];

/**
 * Elements with their own visible text that are clipped, truncated or off the window, whether
 * by their own overflow or by an ancestor's.
 */
async function layoutProblems(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const problems: string[] = [];
    const viewport = document.documentElement.clientWidth;
    const describe = (element: Element, withText = true) => {
      const tag = element.tagName.toLowerCase();
      const cls = element.getAttribute('class');
      const name = `${tag}${cls ? `.${cls.split(/\s+/).join('.')}` : ''}`;
      if (!withText) return name;
      const text = (element.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 60);
      return `${name} "${text}"`;
    };
    const hides = (overflow: string) => overflow === 'hidden' || overflow === 'clip';
    const scrolls = (overflow: string) => overflow === 'auto' || overflow === 'scroll';
    /**
     * Hidden on purpose, for screen readers only: the sr-only class, or the usual
     * visually-hidden recipes of a zero clip rect or an inset(50%) clip path.
     */
    const screenReaderOnly = (element: Element) => {
      if (element.closest('.sr-only')) return true;
      for (let node: Element | null = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (style.clip === 'rect(0px, 0px, 0px, 0px)' || style.clipPath === 'inset(50%)')
          return true;
      }
      return false;
    };
    /** The nearest ancestor whose overflow cuts off part of this element's box, if any. */
    const clippedBy = (element: Element): Element | null => {
      const box = element.getBoundingClientRect();
      let checkX = true;
      let checkY = true;
      let position = getComputedStyle(element).position;
      for (
        let parent = element.parentElement;
        parent && (checkX || checkY);
        parent = parent.parentElement
      ) {
        // A fixed box escapes every ancestor; an absolute one, those inside its containing block.
        if (position === 'fixed') return null;
        const style = getComputedStyle(parent);
        if (position === 'absolute' && style.position === 'static') continue;
        position = style.position;
        // Overflow is cut at the inside of the border.
        const edge = parent.getBoundingClientRect();
        const left = edge.left + parseFloat(style.borderLeftWidth);
        const right = edge.right - parseFloat(style.borderRightWidth);
        const top = edge.top + parseFloat(style.borderTopWidth);
        const bottom = edge.bottom - parseFloat(style.borderBottomWidth);
        if (checkX && hides(style.overflowX) && (box.left < left - 1 || box.right > right + 1))
          return parent;
        if (checkY && hides(style.overflowY) && (box.top < top - 1 || box.bottom > bottom + 1))
          return parent;
        // Inside a scrolling box the text can be scrolled to; stop checking that direction.
        if (scrolls(style.overflowX)) checkX = false;
        if (scrolls(style.overflowY)) checkY = false;
      }
      return null;
    };
    for (const element of Array.from(document.querySelectorAll('body *'))) {
      const style = getComputedStyle(element);
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      const ownText = Array.from(element.childNodes).some(
        (node) => node.nodeType === Node.TEXT_NODE && (node.textContent ?? '').trim(),
      );
      if (!ownText) continue;
      // Not laid out at all (inside a display: none parent), or drawn by the browser (options).
      if (element.getClientRects().length === 0 || element.closest('select')) continue;
      if (screenReaderOnly(element)) continue;
      const box = element.getBoundingClientRect();
      const hidesX = hides(style.overflowX);
      const hidesY = hides(style.overflowY);
      if (hidesX && element.scrollWidth > element.clientWidth + 1)
        problems.push(`clipped sideways: ${describe(element)}`);
      else if (hidesY && element.scrollHeight > element.clientHeight + 1)
        problems.push(`clipped vertically: ${describe(element)}`);
      else if ((hidesX || hidesY) && (box.width <= 1 || box.height <= 1))
        problems.push(`squeezed to ${box.width}x${box.height}px: ${describe(element)}`);
      const clipper = clippedBy(element);
      if (clipper) problems.push(`cut off by ${describe(clipper, false)}: ${describe(element)}`);
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

/** Paragraphs in the cards that run wider than 75 characters, with their width in characters. */
async function longLines(page: Page, selector: string): Promise<string[]> {
  return page.locator(selector).evaluateAll((elements) =>
    elements.flatMap((element) => {
      const probe = document.createElement('span');
      probe.style.cssText = 'position: absolute; width: 1ch';
      element.append(probe);
      const ch = probe.getBoundingClientRect().width;
      probe.remove();
      const characters = Math.round(element.getBoundingClientRect().width / ch);
      const text = (element.textContent ?? '').trim().slice(0, 40);
      return characters > 75 ? [`${characters}ch: "${text}"`] : [];
    }),
  );
}

test.describe('a 2560x1440 window', () => {
  test.use({ viewport: { width: 2560, height: 1440 } });

  test('keeps the run card text to a readable line length', async ({ page }) => {
    await page.goto('/?mock=quota-pause');
    await page.getByRole('button', { name: /Webinar objections/ }).click();
    await expect(page.getByText(/Paused: ChatGPT allowance/)).toBeVisible();
    expect(await longLines(page, '.run-banner p, .run-note')).toEqual([]);
  });

  test('keeps the done card text short and each count near its label', async ({ page }) => {
    await project(/Shipping questions/)(page);
    await expect(page.getByRole('heading', { name: 'Every lead accounted for' })).toBeVisible();
    expect(await longLines(page, '.done-hero > p, .done-panel p')).toEqual([]);
    // A label and its number sit within 30rem (480px), not at the two edges of a wide card.
    const rows = await page
      .locator('.reconciliation-line')
      .evaluateAll((lines) => lines.map((line) => Math.round(line.getBoundingClientRect().width)));
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.filter((width) => width > 480)).toEqual([]);
  });
});

// The check above must itself see clipping: these break the done view on purpose.
test.describe('the layout check', () => {
  test.use({ viewport: { width: 1280, height: 720 } });
  const openDone = async (page: Page) => {
    await project(/Shipping questions/)(page);
    await expect(page.getByRole('heading', { name: 'Worth a look' })).toBeVisible();
    expect(await layoutProblems(page), 'the done view before the break').toEqual([]);
  };

  test('sees text cut off by a parent with overflow hidden', async ({ page }) => {
    await openDone(page);
    await page.addStyleTag({ content: '.done-panel { max-height: 60px; overflow: hidden; }' });
    const problems = await layoutProblems(page);
    expect(
      problems.filter((problem) => problem.startsWith('cut off by section.done-panel')),
    ).not.toEqual([]);
  });

  test('sees text squeezed into a 1px box', async ({ page }) => {
    await openDone(page);
    await page.addStyleTag({ content: '.done-panel h2 { width: 1px; overflow: hidden; }' });
    const problems = await layoutProblems(page);
    expect(problems.filter((problem) => problem.includes('"Worth a look"'))).not.toEqual([]);
  });
});
