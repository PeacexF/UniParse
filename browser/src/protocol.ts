// Mirror of src/uparse/runtime/protocol.py. Both sides must move together.

export const PROTOCOL_VERSION = 1;

export const METHODS = [
  'hello',
  'navigate',
  'content',
  'evaluate',
  'scroll',
  'click',
  'screenshot',
  'close',
  'shutdown',
] as const;

export type Method = (typeof METHODS)[number];

/** Codes the Python side translates into its error taxonomy; anything else becomes WORKER_ERROR. */
export type ErrorCode = 'TIMEOUT' | 'HTTP' | 'NAVIGATION' | 'PROTOCOL' | 'BROWSER' | 'INTERNAL';

export interface Request {
  id: number;
  method: string;
  params: Record<string, unknown>;
}

export type Response =
  | { id: number; ok: true; data: Record<string, unknown> }
  | { id: number; ok: false; error: string; code: ErrorCode };

export class WorkerError extends Error {
  readonly code: ErrorCode;

  constructor(message: string, code: ErrorCode = 'INTERNAL') {
    super(message);
    this.name = 'WorkerError';
    this.code = code;
  }
}

// Captured before guardStdout() reroutes everything else, so frames keep the real channel.
const writeFrame = process.stdout.write.bind(process.stdout);

export function send(response: Response): void {
  writeFrame(`${JSON.stringify(response)}\n`);
}

/**
 * stdout to a pipe is asynchronous: exiting straight after a write can truncate the
 * last frame. Give it a chance to drain, but never hang on it.
 */
export function exitAfterFlush(code: number): void {
  const finish = (): void => process.exit(code);
  if (process.stdout.writableLength === 0) {
    setImmediate(finish);
    return;
  }
  process.stdout.once('drain', finish);
  setTimeout(finish, 2_000).unref();
}

export function log(...parts: unknown[]): void {
  const text = parts.map((p) => (typeof p === 'string' ? p : inspect(p))).join(' ');
  process.stderr.write(`${text}\n`);
}

function inspect(value: unknown): string {
  if (value instanceof Error) return `${value.name}: ${value.message}`;
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

/**
 * stdout is the protocol channel. Redirect every other writer — console, Playwright,
 * a stray dependency — to stderr so a single stray print cannot desynchronize the stream.
 */
export function guardStdout(): void {
  process.stdout.write = ((chunk: string | Uint8Array, ...rest: unknown[]) =>
    (process.stderr.write as (...args: never[]) => boolean)(
      chunk as never,
      ...(rest as never[]),
    )) as typeof process.stdout.write;

  const console_ = console as unknown as Record<string, unknown>;
  for (const name of ['log', 'info', 'warn', 'error', 'debug', 'trace', 'dir']) {
    console_[name] = (...args: unknown[]) => log(...args);
  }
}

export function parseRequest(line: string): Request {
  let payload: unknown;
  try {
    payload = JSON.parse(line);
  } catch (error) {
    throw new WorkerError(`malformed request frame: ${asMessage(error)}`, 'PROTOCOL');
  }
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    throw new WorkerError('request frame must be a JSON object', 'PROTOCOL');
  }
  const frame = payload as Record<string, unknown>;
  const id = frame['id'];
  const method = frame['method'];
  if (typeof id !== 'number' || !Number.isFinite(id)) {
    throw new WorkerError('request frame needs a numeric "id"', 'PROTOCOL');
  }
  if (typeof method !== 'string') {
    throw new WorkerError('request frame needs a string "method"', 'PROTOCOL');
  }
  const params = frame['params'];
  return {
    id,
    method,
    params:
      typeof params === 'object' && params !== null && !Array.isArray(params)
        ? (params as Record<string, unknown>)
        : {},
  };
}

/** Playwright reports timeouts by error name; everything net::* is a navigation failure. */
export function toWorkerError(error: unknown): WorkerError {
  if (error instanceof WorkerError) return error;
  const message = asMessage(error);
  const name = error instanceof Error ? error.name : '';
  if (name === 'TimeoutError' || /\bTimeout\b.*\bexceeded\b/i.test(message)) {
    return new WorkerError(message, 'TIMEOUT');
  }
  if (message.includes('net::') || /ERR_[A-Z_]+/.test(message)) {
    return new WorkerError(message, 'NAVIGATION');
  }
  if (/Target (page|frame|context|browser) .*closed|Browser has been closed/i.test(message)) {
    return new WorkerError(message, 'BROWSER');
  }
  return new WorkerError(message, 'INTERNAL');
}

export function asMessage(error: unknown): string {
  if (error instanceof Error) return error.message || error.name;
  return String(error);
}

// --- param coercion -------------------------------------------------------
// Params arrive as untyped JSON. Read them defensively; never trust a shape.

export function str(params: Record<string, unknown>, key: string): string | undefined {
  const value = params[key];
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

export function requireStr(params: Record<string, unknown>, key: string): string {
  const value = str(params, key);
  if (value === undefined) throw new WorkerError(`"${key}" is required`, 'PROTOCOL');
  return value;
}

export function num(
  params: Record<string, unknown>,
  key: string,
  fallback: number,
): number {
  const value = params[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

export function bool(params: Record<string, unknown>, key: string, fallback = false): boolean {
  const value = params[key];
  return typeof value === 'boolean' ? value : fallback;
}

export function strList(params: Record<string, unknown>, key: string): string[] | undefined {
  const value = params[key];
  if (!Array.isArray(value)) return undefined;
  return value.filter((item): item is string => typeof item === 'string');
}

export function strMap(params: Record<string, unknown>, key: string): Record<string, string> {
  const value = params[key];
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return {};
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    if (typeof v === 'string') out[k] = v;
  }
  return out;
}
