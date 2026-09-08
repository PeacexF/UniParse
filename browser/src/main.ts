import { createInterface } from 'node:readline';

import * as actions from './actions.js';
import { Runtime, readConfig } from './browser.js';
import {
  PROTOCOL_VERSION,
  type Request,
  type Response,
  WorkerError,
  exitAfterFlush,
  exitOnBrokenPipe,
  guardStdout,
  log,
  parseRequest,
  send,
  str,
  toWorkerError,
} from './protocol.js';

const NAME = 'uparse-browser';
const ORPHAN_CHECK_MS = 2_000;
const ORPHAN_EXIT_MS = 3_000;

let runtime: Runtime | undefined;
let stopping = false;
let inflight = 0;

function required(): Runtime {
  if (!runtime) throw new WorkerError('handshake required: call "hello" first', 'PROTOCOL');
  return runtime;
}

async function dispatch(request: Request): Promise<Record<string, unknown>> {
  const { method, params } = request;

  switch (method) {
    case 'hello': {
      runtime = new Runtime(readConfig(params));
      return {
        protocol: PROTOCOL_VERSION,
        name: NAME,
        node: process.version,
        concurrency: runtime.config.concurrency,
        blockResources: [...runtime.config.blockResources],
      };
    }

    case 'navigate': {
      const worker = required();
      const navigation = actions.readNavigateParams(params, worker.config.timeout);
      return worker.withPage(str(params, 'pageId'), async (page, lease) => ({
        ...(await actions.navigate(page, navigation)),
        pageId: lease.id,
      }));
    }

    case 'content':
      return required().withPage(pageId(params), (page) => actions.content(page));

    case 'evaluate':
      return required().withPage(pageId(params), (page) => actions.evaluate(page, params));

    case 'scroll': {
      const worker = required();
      return worker.withPage(pageId(params), (page) =>
        actions.scroll(page, params, worker.config.timeout),
      );
    }

    case 'click': {
      const worker = required();
      return worker.withPage(pageId(params), (page) =>
        actions.click(page, params, worker.config.timeout),
      );
    }

    case 'screenshot':
      return required().withPage(pageId(params), (page) => actions.screenshot(page, params));

    case 'close': {
      await required().close(pageId(params));
      return { closed: true };
    }

    case 'shutdown': {
      stopping = true;
      await runtime?.shutdown();
      runtime = undefined;
      return { ok: true };
    }

    default:
      throw new WorkerError(`unknown method "${method}"`, 'PROTOCOL');
  }
}

function pageId(params: Record<string, unknown>): string {
  const id = str(params, 'pageId');
  if (!id) throw new WorkerError('"pageId" is required (from a previous navigate)', 'PROTOCOL');
  return id;
}

async function handle(request: Request): Promise<void> {
  let response: Response;
  try {
    response = { id: request.id, ok: true, data: await dispatch(request) };
  } catch (error) {
    const failure = toWorkerError(error);
    log(`${request.method} failed [${failure.code}]: ${failure.message}`);
    // Playwright appends a multi-line call log; the summary goes on the wire, the rest to stderr.
    const summary = failure.message.split('\n', 1)[0] ?? failure.message;
    response = { id: request.id, ok: false, error: summary, code: failure.code };
  }
  send(response);

  inflight -= 1;
  if (stopping && inflight === 0) exitAfterFlush(0);
}

/**
 * Exit when the Python side goes away.
 *
 * Closing stdin is the intended signal, but Chromium inherits that pipe: while a browser
 * it launched is still alive the write end stays open, no EOF ever arrives, and the worker
 * keeps running (and burning CPU) long after anything can talk to it. Being reparented to
 * init is the unambiguous fact, so watch for that too.
 */
function watchParent(): void {
  const original = process.ppid;
  // Deliberately not unref'd: this check is the last thing keeping an orphan honest.
  setInterval(() => {
    if (process.ppid === original && process.ppid !== 1) return;
    log(`parent ${original} is gone; exiting`);
    stopping = true;
    // Closing Chromium is worth a try, but never worth waiting on: its pipes are broken
    // too, and a hung close is exactly how a worker ends up outliving everything.
    const hard = setTimeout(() => process.exit(0), ORPHAN_EXIT_MS);
    void runtime
      ?.shutdown()
      .catch(() => undefined)
      .finally(() => {
        clearTimeout(hard);
        process.exit(0);
      });
    if (!runtime) {
      clearTimeout(hard);
      process.exit(0);
    }
  }, ORPHAN_CHECK_MS);
}

function main(): void {
  exitOnBrokenPipe();
  guardStdout();
  watchParent();
  process.stdin.setEncoding('utf8');

  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });

  lines.on('line', (line: string) => {
    const text = line.trim();
    if (!text) return;
    inflight += 1;
    let request: Request;
    try {
      request = parseRequest(text);
    } catch (error) {
      // No id to correlate against, so this can only go to the diagnostics channel.
      inflight -= 1;
      log('dropping frame:', toWorkerError(error).message);
      return;
    }
    void handle(request);
  });

  // Python closing stdin is the normal exit path when it never sends "shutdown".
  lines.on('close', () => {
    stopping = true;
    void shutdown(0);
  });

  for (const signal of ['SIGINT', 'SIGTERM'] as const) {
    process.on(signal, () => {
      stopping = true;
      void shutdown(0);
    });
  }

  process.on('uncaughtException', (error) => {
    log('uncaught:', error);
    void shutdown(1);
  });
  process.on('unhandledRejection', (error) => {
    log('unhandled rejection:', error);
  });
}

async function shutdown(code: number): Promise<void> {
  try {
    await runtime?.shutdown();
  } catch (error) {
    log('shutdown failed:', error);
  }
  exitAfterFlush(code);
}

main();
