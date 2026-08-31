import type { TokenResponse } from "./types";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

let accessToken: string | null = null;
let refreshInFlight: Promise<string | null> | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function getAccessToken() {
  return accessToken;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** FastAPI's `detail` field is a plain string for application-raised
 * HTTPExceptions, but for a 422 request-validation failure (including a
 * Pydantic model_validator raising ValueError, e.g. "provide exactly one
 * of X or Y") it's always a list of {loc, msg, type} objects instead --
 * assigning that straight to an Error's message coerces it to the useless
 * "[object Object]" every caller that renders error.message then shows. */
function formatErrorDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (item && typeof item === "object" && typeof (item as { msg?: unknown }).msg === "string" ? (item as { msg: string }).msg : null))
      .filter((msg): msg is string => msg !== null);
    if (messages.length) return messages.join("; ");
  }
  if (detail && typeof detail === "object" && typeof (detail as { msg?: unknown }).msg === "string") {
    return (detail as { msg: string }).msg;
  }
  return fallback;
}

async function refreshAccessToken(): Promise<string | null> {
  if (!refreshInFlight) {
    refreshInFlight = fetch(`${API_BASE_URL}/api/v1/auth/refresh`, {
      method: "POST",
      credentials: "include",
    })
      .then(async (res) => {
        if (!res.ok) return null;
        const data: TokenResponse = await res.json();
        setAccessToken(data.access_token);
        return data.access_token;
      })
      .catch(() => null)
      .finally(() => {
        refreshInFlight = null;
      });
  }
  return refreshInFlight;
}

interface ApiFetchOptions extends RequestInit {
  skipAuthRetry?: boolean;
}

/** Fetch wrapper that attaches the in-memory access token and transparently
 * retries once via the httpOnly refresh cookie on a 401 (access tokens are
 * short-lived by design; this is what keeps a session alive across page
 * loads and token expiry without storing the token in localStorage). */
export async function apiFetch<T>(path: string, options: ApiFetchOptions = {}): Promise<T> {
  const { skipAuthRetry, headers, ...rest } = options;

  const doFetch = () =>
    fetch(`${API_BASE_URL}${path}`, {
      ...rest,
      credentials: "include",
      headers: {
        ...(rest.body ? { "Content-Type": "application/json" } : {}),
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        ...headers,
      },
    });

  let res = await doFetch();

  if (res.status === 401 && !skipAuthRetry) {
    const newToken = await refreshAccessToken();
    if (newToken) {
      res = await doFetch();
    }
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = formatErrorDetail(body.detail, detail);
    } catch {
      // response had no JSON body
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

/** For endpoints that return a file body (CSV export, backup download)
 * rather than JSON -- apiFetch always parses JSON, so this is a separate
 * path that triggers a browser save using the same auth header. */
export async function apiDownload(path: string, suggestedFilename: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    credentials: "include",
    headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
  });
  if (!res.ok) throw new ApiError(res.status, res.statusText);

  const disposition = res.headers.get("Content-Disposition");
  const match = disposition?.match(/filename="?([^"]+)"?/);
  const filename = match?.[1] ?? suggestedFilename;

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export { refreshAccessToken };
