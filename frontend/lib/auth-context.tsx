"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";

import { apiFetch, refreshAccessToken, setAccessToken, storeCredentialForAutofill } from "./api";
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

  const loadUser = useCallback(async () => {
    try {
      const me = await apiFetch<UserOut>("/api/v1/auth/me");
      setUser(me);
      setStatus("authenticated");
    } catch {
      setUser(null);
      setStatus("unauthenticated");
    }
  }, []);

  useEffect(() => {
    // On mount there's no in-memory access token yet (a fresh page load), so
    // silently exchange the httpOnly refresh cookie (if any) for one before
    // deciding whether the visitor is signed in.
    refreshAccessToken().then((token) => {
      if (token) {
        loadUser();
      } else {
        setStatus("unauthenticated");
      }
    });
  }, [loadUser]);

  const login = useCallback(
    async (email: string, password: string) => {
      const tokenResponse = await apiFetch<TokenResponse>("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
        skipAuthRetry: true,
      });
      setAccessToken(tokenResponse.access_token);
      await loadUser();
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
