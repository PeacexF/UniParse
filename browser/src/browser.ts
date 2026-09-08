import { existsSync } from 'node:fs';
import { randomUUID } from 'node:crypto';

import { chromium } from 'playwright';
import type { Browser, BrowserContext, Page, Route } from 'playwright';

import {
  WorkerError,
  asMessage,
  bool,
  log,
  num,
  str,
  strList,
  strMap,
  toWorkerError,
} from './protocol.js';

/** Hosts whose only job is analytics, tracking or advertising. Blocked unless overridden. */
const DEFAULT_BLOCKED_HOSTS = [
  'google-analytics.com',
  'analytics.google.com',
  'googletagmanager.com',
  'googlesyndication.com',
  'googleadservices.com',
  'doubleclick.net',
  'adservice.google.com',
  'connect.facebook.net',
  'facebook.com/tr',
  'analytics.tiktok.com',
  'hotjar.com',
  'hotjar.io',
  'mixpanel.com',
  'segment.com',
  'segment.io',
  'amplitude.com',
  'fullstory.com',
  'nr-data.net',
  'newrelic.com',
  'scorecardresearch.com',
  'quantserve.com',
  'criteo.com',
  'criteo.net',
  'taboola.com',
  'outbrain.com',
  'adnxs.com',
  'rubiconproject.com',
  'pubmatic.com',
  'casalemedia.com',
  'clarity.ms',
  'bat.bing.com',
];

export interface RuntimeConfig {
  headless: boolean;
  timeout: number;
  concurrency: number;
  userAgent: string | undefined;
  viewport: { width: number; height: number };
  locale: string | undefined;
  timezone: string | undefined;
  blockResources: Set<string>;
  blockHosts: string[];
  extraHeaders: Record<string, string>;
  storageState: string | undefined;
  executablePath: string | undefined;
  persistCookies: boolean;
}

export function readConfig(params: Record<string, unknown>): RuntimeConfig {
  const viewport = params['viewport'];
  const [width, height] = Array.isArray(viewport) ? viewport : [];

  // "document" is never blockable: aborting it would abort the navigation itself.
  const blockResources = new Set(strList(params, 'blockResources') ?? []);
  blockResources.delete('document');

  return {
    headless: bool(params, 'headless', true),
    timeout: num(params, 'timeout', 30_000),
    concurrency: Math.max(1, Math.trunc(num(params, 'concurrency', 4))),
    userAgent: str(params, 'userAgent'),
    viewport: {
      width: typeof width === 'number' ? width : 1366,
      height: typeof height === 'number' ? height : 900,
    },
    locale: str(params, 'locale'),
    timezone: str(params, 'timezone'),
    blockResources,
    blockHosts: strList(params, 'blockHosts') ?? DEFAULT_BLOCKED_HOSTS,
    extraHeaders: strMap(params, 'extraHeaders'),
    storageState: str(params, 'storageState'),
    executablePath: str(params, 'executablePath'),
    persistCookies: bool(params, 'persistCookies', true),
  };
}

/**
 * A leased page. `id` is regenerated every time the underlying page is handed out again,
 * so a stale id from an earlier lease can never silently address someone else's page.
 */
export interface Lease {
  id: string;
  page: Page;
  context: BrowserContext;
  owned: boolean;
  busy: boolean;
}

/**
 * Owns the browser, its contexts and a bounded pool of pages.
 *
 * One Chromium process for the whole job; with `persistCookies` a single context keeps
 * the session, otherwise every lease gets a throwaway context. Pages are pooled up to
 * `concurrency` and reused rather than relaunched.
 */
export class Runtime {
  #config: RuntimeConfig;
  #browser: Browser | undefined;
  #launching: Promise<Browser> | undefined;
  #shared: BrowserContext | undefined;
  #leases = new Map<string, Lease>();
  #idle: Lease[] = [];
  #waiters: Array<() => void> = [];
  #closed = false;

  constructor(config: RuntimeConfig) {
    this.#config = config;
  }

  get config(): RuntimeConfig {
    return this.#config;
  }

  async browser(): Promise<Browser> {
    if (this.#browser) return this.#browser;
    this.#launching ??= this.#launch();
    this.#browser = await this.#launching;
    return this.#browser;
  }

  async #launch(): Promise<Browser> {
    const { headless, executablePath } = this.#config;
    log(`launching chromium (headless=${headless})`);
    try {
      const browser = await chromium.launch({
        headless,
        ...(executablePath ? { executablePath } : {}),
        args: ['--disable-dev-shm-usage'],
      });
      browser.on('disconnected', () => {
        log('chromium disconnected');
        this.#browser = undefined;
        this.#launching = undefined;
        this.#shared = undefined;
        this.#leases.clear();
        this.#idle = [];
      });
      return browser;
    } catch (error) {
      throw new WorkerError(
        `cannot launch chromium: ${asMessage(error)} (run \`make browser-install\`)`,
        'BROWSER',
      );
    }
  }

  async #newContext(): Promise<BrowserContext> {
    const config = this.#config;
    const browser = await this.browser();
    const context = await browser.newContext({
      viewport: config.viewport,
      ...(config.userAgent ? { userAgent: config.userAgent } : {}),
      ...(config.locale ? { locale: config.locale } : {}),
      ...(config.timezone ? { timezoneId: config.timezone } : {}),
      ...(config.storageState && existsSync(config.storageState)
        ? { storageState: config.storageState }
        : {}),
    });
    context.setDefaultTimeout(config.timeout);
    context.setDefaultNavigationTimeout(config.timeout);
    if (Object.keys(config.extraHeaders).length > 0) {
      await context.setExtraHTTPHeaders(config.extraHeaders);
    }
    await this.#installBlocking(context);
    return context;
  }

  async #installBlocking(context: BrowserContext): Promise<void> {
    const { blockResources, blockHosts } = this.#config;
    if (blockResources.size === 0 && blockHosts.length === 0) return;
    await context.route('**/*', (route: Route) => {
      const request = route.request();
      if (blockResources.has(request.resourceType()) || isBlockedHost(request.url(), blockHosts)) {
        void route.abort('blockedbyclient').catch(() => undefined);
        return;
      }
      void route.continue().catch(() => undefined);
    });
  }

  async #context(): Promise<{ context: BrowserContext; owned: boolean }> {
    if (!this.#config.persistCookies) {
      return { context: await this.#newContext(), owned: true };
    }
    this.#shared ??= await this.#newContext();
    return { context: this.#shared, owned: false };
  }

  /** Take a page from the pool, growing it up to `concurrency`, else waiting for a release. */
  async acquire(): Promise<Lease> {
    for (;;) {
      if (this.#closed) throw new WorkerError('worker is shutting down', 'BROWSER');
      const idle = this.#idle.pop();
      if (idle) {
        this.#leases.delete(idle.id);
        idle.id = randomUUID();
        idle.busy = true;
        this.#leases.set(idle.id, idle);
        return idle;
      }
      if (this.#leases.size < this.#config.concurrency) return this.#create();
      await new Promise<void>((resolve) => this.#waiters.push(resolve));
    }
  }

  /** Re-take a specific page by id, for follow-up actions on a page already navigated. */
  acquireById(id: string): Lease {
    const lease = this.#leases.get(id);
    if (!lease) {
      throw new WorkerError(`unknown page "${id}" (closed or recycled)`, 'PROTOCOL');
    }
    if (lease.busy) throw new WorkerError(`page "${id}" is busy`, 'PROTOCOL');
    this.#idle = this.#idle.filter((candidate) => candidate !== lease);
    lease.busy = true;
    return lease;
  }

  async #create(): Promise<Lease> {
    const { context, owned } = await this.#context();
    const page = await context.newPage();
    page.setDefaultTimeout(this.#config.timeout);
    page.setDefaultNavigationTimeout(this.#config.timeout);
    const lease: Lease = { id: randomUUID(), page, context, owned, busy: true };
    this.#leases.set(lease.id, lease);
    return lease;
  }

  release(lease: Lease): void {
    if (!this.#leases.has(lease.id)) return;
    lease.busy = false;
    if (lease.page.isClosed()) {
      void this.close(lease.id);
      return;
    }
    this.#idle.push(lease);
    this.#wake();
  }

  async close(id: string): Promise<void> {
    const lease = this.#leases.get(id);
    if (!lease) return;
    this.#leases.delete(id);
    this.#idle = this.#idle.filter((candidate) => candidate !== lease);
    await lease.page.close().catch(() => undefined);
    if (lease.owned) await lease.context.close().catch(() => undefined);
    this.#wake();
  }

  #wake(): void {
    this.#waiters.shift()?.();
  }

  async shutdown(): Promise<void> {
    this.#closed = true;
    for (const waiter of this.#waiters.splice(0)) waiter();
    for (const id of [...this.#leases.keys()]) await this.close(id);
    if (this.#shared) {
      await this.#saveStorageState(this.#shared);
      await this.#shared.close().catch(() => undefined);
      this.#shared = undefined;
    }
    const browser = this.#browser;
    this.#browser = undefined;
    this.#launching = undefined;
    if (browser) await browser.close().catch((error) => log('browser close failed:', error));
  }

  async #saveStorageState(context: BrowserContext): Promise<void> {
    const { storageState, persistCookies } = this.#config;
    if (!storageState || !persistCookies) return;
    try {
      await context.storageState({ path: storageState });
    } catch (error) {
      log('cannot persist storage state:', error);
    }
  }

  /**
   * Run `action` against a leased page, always giving the page back.
   *
   * A page that failed is discarded rather than pooled: a goto that timed out leaves its
   * navigation in flight, and the next goto on that page dies with net::ERR_ABORTED.
   * A fresh page costs one round trip; a poisoned one costs the rest of the job.
   */
  async withPage<T>(
    id: string | undefined,
    action: (page: Page, lease: Lease) => Promise<T>,
  ): Promise<T> {
    const lease = id ? this.acquireById(id) : await this.acquire();
    try {
      const result = await action(lease.page, lease);
      this.release(lease);
      return result;
    } catch (error) {
      lease.busy = false;
      await this.close(lease.id);
      throw toWorkerError(error);
    }
  }
}

function isBlockedHost(url: string, blocked: string[]): boolean {
  let target: URL;
  try {
    target = new URL(url);
  } catch {
    return false;
  }
  const host = target.hostname;
  const hostPath = `${host}${target.pathname}`;
  return blocked.some(
    (entry) =>
      host === entry || host.endsWith(`.${entry}`) || (entry.includes('/') && hostPath.startsWith(entry)),
  );
}
