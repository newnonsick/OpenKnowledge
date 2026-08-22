import createClient from "openapi-fetch";

import type { paths } from "@/lib/generated/openapi";

export type ApiErrorPayload = {
  error?: {
    code?: string;
    message?: string;
    type?: string;
  };
  request_id?: string;
};

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;

  constructor(status: number, payload: ApiErrorPayload) {
    super(payload.error?.message || "The request could not be completed.");
    this.name = "ApiError";
    this.status = status;
    this.code = payload.error?.code || "request_failed";
    this.requestId = payload.request_id;
    if (this.code === "recent_authentication_required" && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("aigw-step-up-required"));
    }
  }
}

type ApiRequestOptions = {
  body?: unknown;
  headers?: HeadersInit;
  idempotent?: boolean;
  method?: "DELETE" | "GET" | "PATCH" | "POST" | "PUT";
  retryAuthentication?: boolean;
  signal?: AbortSignal;
};

type ApiMultipartOptions = {
  headers?: HeadersInit;
  idempotent?: boolean;
  retryAuthentication?: boolean;
  signal?: AbortSignal;
};

export type RefreshResponse = {
  status: "refreshed";
  access_expires_at: string;
};

let refreshInFlight: Promise<RefreshResponse> | null = null;
let meaningfulActivityAt = 0;
const refreshCoordinationWindow = 30 * 1000;
const meaningfulActivityWindow = 2 * 60 * 1000;
const refreshRecordKey = "aigw-last-session-refresh";

type SharedRefreshRecord = RefreshResponse & { completed_at: number };

function traceparent(): string {
  const traceId = globalThis.crypto.randomUUID().replaceAll("-", "");
  const spanId = globalThis.crypto.randomUUID().replaceAll("-", "").slice(0, 16);
  return `00-${traceId}-${spanId}-01`;
}

function tracedHeaders(value?: HeadersInit): Headers {
  const headers = new Headers(value);
  if (!headers.has("traceparent")) {
    headers.set("traceparent", traceparent());
  }
  return headers;
}

export function noteMeaningfulActivity(): void {
  meaningfulActivityAt = Date.now();
}

function markMeaningfulActivity(headers: Headers, path: string): void {
  if (!path.startsWith("/api/v1/auth/") && Date.now() - meaningfulActivityAt <= meaningfulActivityWindow) {
    headers.set("X-AIGW-Meaningful-Activity", "1");
  }
}

function csrfToken(): string | null {
  if (typeof document === "undefined") {
    return null;
  }
  const value = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("aigw-csrf="));
  return value ? decodeURIComponent(value.slice("aigw-csrf=".length)) : null;
}

async function decodeResponse<T>(response: Response): Promise<T> {
  if (response.status === 204) {
    return undefined as T;
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(response.status, payload as ApiErrorPayload);
  }
  return payload as T;
}

async function rawRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const method = options.method || "GET";
  const headers = tracedHeaders(options.headers);
  markMeaningfulActivity(headers, path);
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  if (method !== "GET") {
    const token = csrfToken();
    if (token) {
      headers.set("X-CSRF-Token", token);
    }
  }
  if (options.idempotent) {
    headers.set("Idempotency-Key", globalThis.crypto.randomUUID());
  }
  const response = await fetch(path, {
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    cache: "no-store",
    credentials: "include",
    headers,
    method,
    signal: options.signal,
  });
  return decodeResponse<T>(response);
}

function sharedRefreshRecord(): SharedRefreshRecord | null {
  if (typeof localStorage === "undefined") {
    return null;
  }
  try {
    const value = JSON.parse(localStorage.getItem(refreshRecordKey) || "null") as SharedRefreshRecord | null;
    return value && value.status === "refreshed" && typeof value.completed_at === "number" ? value : null;
  } catch {
    return null;
  }
}

async function rotateSession(): Promise<RefreshResponse> {
  const recent = sharedRefreshRecord();
  if (recent && Date.now() - recent.completed_at < refreshCoordinationWindow) {
    return recent;
  }
  const refreshed = await rawRequest<RefreshResponse>("/api/v1/auth/refresh", {
    body: {},
    method: "POST",
    retryAuthentication: false,
  });
  localStorage.setItem(refreshRecordKey, JSON.stringify({ ...refreshed, completed_at: Date.now() }));
  return refreshed;
}

export function refreshSession(): Promise<RefreshResponse> {
  if (refreshInFlight) {
    return refreshInFlight;
  }
  if (typeof navigator === "undefined" || !navigator.locks) {
    return Promise.reject(new ApiError(409, {
      error: {
        code: "refresh_coordination_unavailable",
        message: "Safe session refresh is unavailable in this browser.",
        type: "conflict_error",
      },
    }));
  }
  const coordinated = navigator.locks.request("aigw-session-refresh", rotateSession) as unknown as Promise<RefreshResponse>;
  const inFlight = coordinated.finally(() => {
    refreshInFlight = null;
  });
  refreshInFlight = inFlight;
  return inFlight;
}

export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const tracedOptions = { ...options, headers: tracedHeaders(options.headers) };
  try {
    return await rawRequest<T>(path, tracedOptions);
  } catch (error) {
    if (
      error instanceof ApiError &&
      error.status === 401 &&
      tracedOptions.retryAuthentication !== false &&
      (tracedOptions.method === undefined || tracedOptions.method === "GET" || tracedOptions.idempotent === true) &&
      !path.startsWith("/api/v1/auth/")
    ) {
      await refreshSession();
      return rawRequest<T>(path, { ...tracedOptions, retryAuthentication: false });
    }
    throw error;
  }
}

export async function apiMultipart<T>(
  path: string,
  body: FormData,
  options: ApiMultipartOptions = {},
): Promise<T> {
  const baseHeaders = tracedHeaders(options.headers);
  if (options.idempotent) {
    baseHeaders.set("Idempotency-Key", globalThis.crypto.randomUUID());
  }
  const execute = () => {
    const headers = new Headers(baseHeaders);
    const token = csrfToken();
    if (token) {
      headers.set("X-CSRF-Token", token);
    }
    return fetch(path, {
      body,
      cache: "no-store",
      credentials: "include",
      headers,
      method: "POST",
      signal: options.signal,
    }).then(decodeResponse<T>);
  };
  try {
    return await execute();
  } catch (error) {
    if (
      error instanceof ApiError &&
      error.status === 401 &&
      options.retryAuthentication !== false &&
      options.idempotent === true
    ) {
      await refreshSession();
      return execute();
    }
    throw error;
  }
}

function prepareContractRequest(request: Request): Request {
  const headers = tracedHeaders(request.headers);
  markMeaningfulActivity(headers, new URL(request.url).pathname);
  if (request.method !== "GET" && request.method !== "HEAD") {
    const token = csrfToken();
    if (token) {
      headers.set("X-CSRF-Token", token);
    }
  }
  return new Request(request, {
    cache: "no-store",
    credentials: "include",
    headers,
  });
}

const contractFetch: typeof fetch = async (input, init) => {
  const initial = prepareContractRequest(
    input instanceof Request ? input : new Request(input, init),
  );
  const retry = initial.clone();
  let response = await fetch(initial);
  const pathname = new URL(initial.url).pathname;
  const retryable = initial.method === "GET" || initial.method === "HEAD" || initial.headers.has("Idempotency-Key");
  if (response.status === 401 && retryable && !pathname.startsWith("/api/v1/auth/")) {
    await refreshSession();
    response = await fetch(prepareContractRequest(retry));
  }
  return response;
};

export const contractClient = createClient<paths>({
  baseUrl: typeof location === "undefined" ? "http://localhost" : location.origin,
  fetch: contractFetch,
});

export async function contractData<T>(
  pending: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const result = await pending;
  if (result.data !== undefined || result.response.ok) {
    return result.data as T;
  }
  throw new ApiError(result.response.status, (result.error || {}) as ApiErrorPayload);
}

export function idempotencyKey(): string {
  return globalThis.crypto.randomUUID();
}
