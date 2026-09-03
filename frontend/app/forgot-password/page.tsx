"use client";

import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { apiFetch, ApiError } from "@/lib/api";
import type { ForgotPasswordRequest } from "@/lib/types";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await apiFetch("/api/v1/auth/forgot-password", {
        method: "POST",
        body: JSON.stringify({ email } satisfies ForgotPasswordRequest),
        skipAuthRetry: true,
      });
      setSubmitted(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to submit request. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex h-screen w-full items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center gap-2">
          <div className="h-3 w-3 rounded-full bg-brand" />
          <h1 className="text-lg font-semibold text-text-primary">TradingMaster</h1>
          <p className="text-sm text-text-muted">Reset your password</p>
        </div>

        {submitted ? (
          <div className="space-y-4 rounded-lg border border-border bg-surface p-6 text-center">
            <p className="text-sm text-text-primary">
              If that account exists, an administrator has been notified and will set a new password for you.
            </p>
            <Link href="/login" className="text-sm text-brand hover:underline">
              Back to sign in
            </Link>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4 rounded-lg border border-border bg-surface p-6">
            <p className="text-sm text-text-muted">
              Enter your email and an administrator will set a new password for your account.
            </p>

            <div className="space-y-1.5">
              <label htmlFor="email" className="text-sm font-medium text-text-secondary">
                Email
              </label>
              <Input
                id="email"
                name="email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>

            {error && (
              <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative" role="alert">
                {error}
              </div>
            )}

            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? "Submitting..." : "Request password reset"}
            </Button>

            <p className="text-center text-sm text-text-muted">
              <Link href="/login" className="text-brand hover:underline">
                Back to sign in
              </Link>
            </p>
          </form>
        )}
      </div>
    </div>
  );
}
