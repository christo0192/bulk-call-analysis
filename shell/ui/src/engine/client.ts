import type { Method, ParamsOf, ResultOf, Notification } from './types';
export type Unsubscribe = () => void;
export interface EngineBridge {
  call<M extends Method>(method: M, params: ParamsOf<M>): Promise<ResultOf<M>>;
  onNotification(cb: (n: Notification) => void): Unsubscribe;
  onStatus(cb: (s: { status: string; reason?: string }) => void): Unsubscribe;
  status(): Promise<string>;
}
export class EngineError extends Error {
  constructor(
    public readonly code: number | null,
    message: string,
    public readonly reason: string | null = null,
    public readonly data: unknown = null,
  ) {
    super(message);
    this.name = 'EngineError';
  }
  static from(error: unknown): EngineError {
    const value = error instanceof Error ? error.message : error;
    if (typeof value === 'object' && value !== null) {
      const parsed = value as { code?: unknown; message?: unknown; data?: unknown };
      if (parsed.code === null || typeof parsed.code === 'number') {
        const data = parsed.data;
        const reason =
          typeof data === 'object' &&
          data !== null &&
          typeof (data as { reason?: unknown }).reason === 'string'
            ? (data as { reason: string }).reason
            : null;
        return new EngineError(
          parsed.code,
          typeof parsed.message === 'string' ? parsed.message : String(error),
          reason,
          data ?? null,
        );
      }
    }
    const message = String(value);
    try {
      const parsed = JSON.parse(message) as {
        code?: number | null;
        message?: string;
        data?: { reason?: string };
      };
      if (parsed.code === null || typeof parsed.code === 'number')
        return new EngineError(
          parsed.code ?? null,
          parsed.message ?? message,
          parsed.data?.reason ?? null,
          parsed.data ?? null,
        );
    } catch {
      /* shell errors may be plain text */
    }
    return new EngineError(null, message);
  }
}
