"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";

import { ApiError, apiFetch, refreshAccessToken, setAccessToken, storeCredentialForAutofill } from "./api";
import type { TokenResponse, UserOut } from "./types";

interface AuthContextValue {
  user: UserOut | null;
  status: "loading" | "authenticated" | "unauthenticated";
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  hasRole: (...roles: string[]) => boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<UserOut | null>(null);
  const [status, setStatus] = useState<"loading" | "authenticated" | "unauthenticated">("loading");

  const loadUser = useCallback(async (): Promise<boolean> => {
    try {
      const me = await apiFetch<UserOut>("/api/v1/auth/me");
      setUser(me);
      setStatus("authenticated");
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setUser(null);
        setStatus("unauthenticated");
        return true;
      }
      return false; // the server couldn't answer: not signed out, try again
    }
  }, []);

  useEffect(() => {
    // On mount there's no in-memory access token yet (a fresh page load), so
    // silently exchange the httpOnly refresh cookie (if any) for one before
    // deciding whether the visitor is signed in. Only a "no valid session"
    // answer signs out; a renewal or /me that couldn't complete is retried
    // (every few seconds, for about a minute) with the page still loading.
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const attempt = async (tries: number) => {
      let settled = false;
      try {
        const token = await refreshAccessToken();
        if (cancelled) return;
        if (token) settled = await loadUser();
        else {
          setStatus("unauthenticated");
          settled = true;
        }
      } catch {
        // SessionUnavailableError: fall through to a retry
      }
      if (cancelled || settled) return;
      if (tries >= 12) setStatus("unauthenticated");
      else timer = setTimeout(() => attempt(tries + 1), 5000);
    };
    attempt(1);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [loadUser]);

  const login = useCallback(
    async (email: string, password: string) => {
      const tokenResponse = await apiFetch<TokenResponse>("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
        skipAuthRetry: true,
      });
      setAccessToken(tokenResponse.access_token);
      if (!(await loadUser())) throw new ApiError(503, "Signed in, but couldn't load your account just now -- try again");
      await storeCredentialForAutofill(email, password);
    },
    [loadUser],
  );

  const logout = useCallback(async () => {
    await apiFetch("/api/v1/auth/logout", { method: "POST", skipAuthRetry: true }).catch(() => undefined);
    setAccessToken(null);
    setUser(null);
    setStatus("unauthenticated");
  }, []);

  const hasRole = useCallback((...roles: string[]) => !!user && user.roles.some((r) => roles.includes(r)), [user]);

  return <AuthContext.Provider value={{ user, status, login, logout, hasRole }}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
