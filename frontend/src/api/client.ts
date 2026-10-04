import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)(?:__Host-)?docnest_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

type ReauthHandler = () => Promise<boolean>;
let reauthHandler: ReauthHandler | null = null;
let unauthorizedHandler: (() => void) | null = null;

export function setReauthHandler(handler: ReauthHandler | null) {
  reauthHandler = handler;
}
export function setUnauthorizedHandler(handler: (() => void) | null) {
  unauthorizedHandler = handler;
}

const csrfMiddleware: Middleware = {
  onRequest({ request }) {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method)) {
      request.headers.set("X-CSRFToken", csrfToken());
    }
    return request;
  },
};

export const client = createClient<paths>({ baseUrl: "", credentials: "same-origin" });
client.use(csrfMiddleware);

export async function ensureCsrf() {
  if (!csrfToken()) await fetch("/api/v1/csrf", { credentials: "same-origin" });
}

function detailOf(error: unknown, fallback: string): string {
  if (error && typeof error === "object" && "detail" in error) {
    const d = (error as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x) => (x && typeof x === "object" && "msg" in x ? x.msg : String(x))).join(", ");
  }
  return fallback;
}

/**
 * Unwrap an openapi-fetch call: returns data or throws ApiError.
 * A 403 "reauthentication_required" triggers the password prompt and one retry.
 */
export async function call<T>(
  fn: () => Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  let result = await fn();
  if (result.response.status === 403 && detailOf(result.error, "") === "reauthentication_required" && reauthHandler) {
    if (await reauthHandler()) result = await fn();
  }
  if (result.response.status === 401 && unauthorizedHandler) unauthorizedHandler();
  if (result.error !== undefined || !result.response.ok) {
    throw new ApiError(result.response.status, detailOf(result.error, `Request failed (${result.response.status})`));
  }
  return result.data as T;
}
