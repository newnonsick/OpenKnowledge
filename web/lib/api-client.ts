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
  const headers = new Headers(options.headers);
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

export function refreshSession(): Promise<RefreshResponse> {
  if (refreshInFlight) {
    return refreshInFlight;
  }
  refreshInFlight = rawRequest<RefreshResponse>("/api/v1/auth/refresh", {
    body: {},
    method: "POST",
    retryAuthentication: false,
  }).finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  try {
    return await rawRequest<T>(path, options);
  } catch (error) {
    if (
      error instanceof ApiError &&
      error.status === 401 &&
      options.retryAuthentication !== false &&
      !path.startsWith("/api/v1/auth/")
    ) {
      await refreshSession();
      return rawRequest<T>(path, { ...options, retryAuthentication: false });
    }
    throw error;
  }
}

export async function apiMultipart<T>(
  path: string,
  body: FormData,
  options: ApiMultipartOptions = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  const token = csrfToken();
  if (token) {
    headers.set("X-CSRF-Token", token);
  }
  if (options.idempotent) {
    headers.set("Idempotency-Key", globalThis.crypto.randomUUID());
  }
  const execute = () => fetch(path, {
      body,
      cache: "no-store",
      credentials: "include",
      headers,
      method: "POST",
      signal: options.signal,
    }).then(decodeResponse<T>);
  try {
    return await execute();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401 && options.retryAuthentication !== false) {
      await refreshSession();
      return execute();
    }
    throw error;
  }
}
