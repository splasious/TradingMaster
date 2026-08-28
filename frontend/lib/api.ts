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
      detail = body.detail ?? detail;
    } catch {
      // response had no JSON body
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export { refreshAccessToken };
