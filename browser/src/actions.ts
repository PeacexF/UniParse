import type { Page } from 'playwright';

import { WorkerError, bool, log, num, requireStr, str } from './protocol.js';

type WaitUntil = 'load' | 'domcontentloaded' | 'networkidle' | 'commit';

const WAIT_UNTIL = new Set<WaitUntil>(['load', 'domcontentloaded', 'networkidle', 'commit']);
const SCROLL_SETTLE_MS = 350;

export interface NavigateParams {
  url: string;
  waitUntil: WaitUntil;
  timeout: number;
  waitFor: string | undefined;
  waitMs: number;
  scroll: boolean;
  maxScrolls: number;
  maxItems: number;
  maxDurationMs: number;
}

export function readNavigateParams(
  params: Record<string, unknown>,
  defaultTimeout: number,
): NavigateParams {
  const raw = str(params, 'waitUntil');
  const timeout = num(params, 'timeout', defaultTimeout);
  return {
    url: requireStr(params, 'url'),
    waitUntil: raw && WAIT_UNTIL.has(raw as WaitUntil) ? (raw as WaitUntil) : 'domcontentloaded',
    timeout,
    waitFor: str(params, 'waitFor'),
    waitMs: Math.max(0, num(params, 'waitMs', 0)),
    scroll: bool(params, 'scroll', false),
    maxScrolls: Math.max(0, Math.trunc(num(params, 'maxScrolls', 20))),
    maxItems: Math.max(0, Math.trunc(num(params, 'maxItems', 0))),
    maxDurationMs: Math.max(1_000, num(params, 'maxDurationMs', timeout * 2)),
  };
}

/**
 * Load a page and hand back its settled HTML.
 *
 * A non-2xx status is *not* an error here: the Python side decides what a 404 or a
 * challenge page means, and it needs the body to do it.
 */
export async function navigate(page: Page, params: NavigateParams): Promise<Record<string, unknown>> {
  const response = await page.goto(params.url, {
    waitUntil: params.waitUntil,
    timeout: params.timeout,
  });

  if (params.waitFor) {
    await page.waitForSelector(params.waitFor, { timeout: params.timeout, state: 'attached' });
  }
  if (params.waitMs > 0) await page.waitForTimeout(params.waitMs);

  const scrolls = params.scroll ? await autoScroll(page, params) : 0;

  return {
    url: page.url(),
    status: response ? response.status() : null,
    title: await page.title().catch(() => null),
    html: await page.content(),
    scrolls,
  };
}

interface Measurement {
  items: number;
  height: number;
}

/**
 * Infinite scroll: measure, scroll, wait, measure again; stop as soon as nothing grew.
 * Always bounded by maxScrolls / maxItems / maxDurationMs — never scroll unbounded.
 */
export async function autoScroll(
  page: Page,
  limits: Pick<NavigateParams, 'maxScrolls' | 'maxItems' | 'maxDurationMs'>,
): Promise<number> {
  const deadline = Date.now() + limits.maxDurationMs;
  let previous = await measure(page);
  let scrolls = 0;

  while (scrolls < limits.maxScrolls && Date.now() < deadline) {
    if (limits.maxItems > 0 && previous.items >= limits.maxItems) break;

    await page.evaluate(() => {
      window.scrollTo(0, document.documentElement.scrollHeight);
    });
    scrolls += 1;

    await page.waitForTimeout(SCROLL_SETTLE_MS);
    await page.waitForLoadState('networkidle', { timeout: 2_000 }).catch(() => undefined);

    const current = await measure(page);
    if (current.items <= previous.items && current.height <= previous.height) break;
    previous = current;
  }

  if (scrolls >= limits.maxScrolls) log(`scroll limit reached (${scrolls})`);
  return scrolls;
}

function measure(page: Page): Promise<Measurement> {
  return page.evaluate(() => ({
    items: document.querySelectorAll('*').length,
    height: document.documentElement.scrollHeight,
  }));
}

export async function content(page: Page): Promise<Record<string, unknown>> {
  return {
    url: page.url(),
    title: await page.title().catch(() => null),
    html: await page.content(),
  };
}

/**
 * Evaluate an expression in the page. The caller already controls the browser through
 * this worker, so this adds no privilege — it is the escape hatch for odd pages.
 */
export async function evaluate(
  page: Page,
  params: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  // Playwright evaluates a bare expression ("document.title") or a function
  // source ("(arg) => ...") from the same string form, so pass it straight through.
  const expression = requireStr(params, 'expression');
  const result = await page.evaluate<unknown, unknown>(expression, params['arg'] ?? null);
  return { result: serializable(result) };
}

export async function scroll(
  page: Page,
  params: Record<string, unknown>,
  defaultTimeout: number,
): Promise<Record<string, unknown>> {
  const scrolls = await autoScroll(page, {
    maxScrolls: Math.max(1, Math.trunc(num(params, 'maxScrolls', 10))),
    maxItems: Math.max(0, Math.trunc(num(params, 'maxItems', 0))),
    maxDurationMs: Math.max(1_000, num(params, 'maxDurationMs', defaultTimeout * 2)),
  });
  return { scrolls, url: page.url() };
}

export async function click(
  page: Page,
  params: Record<string, unknown>,
  defaultTimeout: number,
): Promise<Record<string, unknown>> {
  const selector = requireStr(params, 'selector');
  const timeout = num(params, 'timeout', defaultTimeout);
  const before = page.url();

  await page.click(selector, { timeout });

  if (bool(params, 'waitForNavigation', false)) {
    await page.waitForLoadState('domcontentloaded', { timeout }).catch(() => undefined);
  }
  const waitFor = str(params, 'waitFor');
  if (waitFor) await page.waitForSelector(waitFor, { timeout, state: 'attached' });

  return { url: page.url(), navigated: page.url() !== before };
}

export async function screenshot(
  page: Page,
  params: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const path = str(params, 'path');
  const buffer = await page.screenshot({
    fullPage: bool(params, 'fullPage', true),
    type: 'png',
    ...(path ? { path } : {}),
  });
  return path ? { path, bytes: buffer.length } : { base64: buffer.toString('base64') };
}

/** Keep only what survives a JSON round trip; a DOM handle must never reach the pipe. */
function serializable(value: unknown): unknown {
  try {
    return JSON.parse(JSON.stringify(value ?? null)) as unknown;
  } catch {
    throw new WorkerError('evaluate returned a value that is not JSON-serializable', 'PROTOCOL');
  }
}
