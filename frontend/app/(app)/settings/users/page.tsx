"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, LoadingState } from "@/components/ui/data-state";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Select } from "@/components/ui/select";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiFetch, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useUsers } from "@/lib/hooks";
import { ROLES, type AdminResetPassword, type UserApprove, type UserCreate, type UserOut } from "@/lib/types";

function PendingUserRow({ user }: { user: UserOut }) {
  const queryClient = useQueryClient();
  const [role, setRole] = useState<string>("viewer");
  const [error, setError] = useState<string | null>(null);

  const approveMutation = useMutation({
    mutationFn: () =>
      apiFetch<UserOut>(`/api/v1/users/${user.id}/approve`, {
        method: "POST",
        body: JSON.stringify({ roles: [role] } satisfies UserApprove),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["users"] }),
    onError: (err) => setError(err instanceof ApiError ? err.message : "Failed to approve user"),
  });

  const rejectMutation = useMutation({
    mutationFn: () => apiFetch(`/api/v1/users/${user.id}/reject`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["users"] }),
    onError: (err) => setError(err instanceof ApiError ? err.message : "Failed to reject user"),
  });

  return (
    <tr>
      <Td>{user.full_name}</Td>
      <Td className="text-text-secondary">{user.email}</Td>
      <Td>
        <Select value={role} onChange={(e) => setRole(e.target.value)} className="w-32">
          {ROLES.map((r) => (
            <option key={r} value={r} className="capitalize">
              {r}
            </option>
          ))}
        </Select>
      </Td>
      <Td>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            onClick={() => approveMutation.mutate()}
            disabled={approveMutation.isPending || rejectMutation.isPending}
          >
            {approveMutation.isPending ? "Approving..." : "Approve"}
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => rejectMutation.mutate()}
            disabled={approveMutation.isPending || rejectMutation.isPending}
          >
            {rejectMutation.isPending ? "Rejecting..." : "Reject"}
          </Button>
        </div>
        {error && <p className="mt-1 text-xs text-negative">{error}</p>}
      </Td>
    </tr>
  );
}

function ResetPasswordModal({ user, onClose }: { user: UserOut | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [newPassword, setNewPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  const resetMutation = useMutation({
    mutationFn: () =>
      apiFetch<UserOut>(`/api/v1/users/${user!.id}/reset-password`, {
        method: "POST",
        body: JSON.stringify({ new_password: newPassword } satisfies AdminResetPassword),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["users"] });
      setNewPassword("");
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Failed to reset password"),
  });

  return (
    <Modal open={user !== null} onClose={onClose} title={`Reset Password${user ? ` — ${user.full_name}` : ""}`}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          resetMutation.mutate();
        }}
        className="space-y-4"
      >
        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">New password</label>
          <Input
            required
            type="password"
            minLength={8}
            autoComplete="new-password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
          />
          <p className="text-xs text-text-muted">Share this with the user directly -- they can change it after signing in.</p>
        </div>

        {error && <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">{error}</div>}

        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={resetMutation.isPending}>
            {resetMutation.isPending ? "Resetting..." : "Set new password"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function CreateUserModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<string>("viewer");
  const [error, setError] = useState<string | null>(null);

  const createMutation = useMutation({
    mutationFn: () =>
      apiFetch<UserOut>("/api/v1/users", {
        method: "POST",
        body: JSON.stringify({ email, password, full_name: fullName, roles: [role] } satisfies UserCreate),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["users"] });
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Failed to create user"),
  });

  return (
    <Modal open={open} onClose={onClose} title="Create User">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          createMutation.mutate();
        }}
        className="space-y-4"
      >
        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Full name</label>
          <Input required value={fullName} onChange={(e) => setFullName(e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Email</label>
          <Input required type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Password</label>
          <Input required type="password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Role</label>
          <Select value={role} onChange={(e) => setRole(e.target.value)}>
            {ROLES.map((r) => (
              <option key={r} value={r} className="capitalize">
                {r}
              </option>
            ))}
          </Select>
        </div>

        {error && <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">{error}</div>}

        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={createMutation.isPending}>
            {createMutation.isPending ? "Creating..." : "Create user"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export default function UsersSettingsPage() {
  const { hasRole } = useAuth();
  const { data: users, isLoading, isError } = useUsers();
  const [modalOpen, setModalOpen] = useState(false);
  const [resetTarget, setResetTarget] = useState<UserOut | null>(null);

  if (!hasRole("administrator")) {
    return (
      <div className="rounded-lg border border-border bg-surface p-8 text-center text-sm text-text-muted">
        Only administrators can manage users.
      </div>
    );
  }

  const pendingUsers = users?.filter((u) => !u.is_approved) ?? [];
  const approvedUsers = users?.filter((u) => u.is_approved) ?? [];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Users</h1>
          <p className="text-sm text-text-muted">RBAC is enforced on both the frontend and the API.</p>
        </div>
        <Button onClick={() => setModalOpen(true)}>Create User</Button>
      </div>

      {pendingUsers.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Pending Approval</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <Table>
              <Thead>
                <tr>
                  <Th>Name</Th>
                  <Th>Email</Th>
                  <Th>Role to assign</Th>
                  <Th>Action</Th>
                </tr>
              </Thead>
              <Tbody>
                {pendingUsers.map((u) => (
                  <PendingUserRow key={u.id} user={u} />
                ))}
              </Tbody>
            </Table>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>All Users</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <LoadingState />
          ) : isError ? (
            <ErrorState description="Could not load users." />
          ) : (
            <Table>
              <Thead>
                <tr>
                  <Th>Name</Th>
                  <Th>Email</Th>
                  <Th>Roles</Th>
                  <Th>Status</Th>
                  <Th>Action</Th>
                </tr>
              </Thead>
              <Tbody>
                {approvedUsers.map((u) => (
                  <tr key={u.id}>
                    <Td>{u.full_name}</Td>
                    <Td className="text-text-secondary">{u.email}</Td>
                    <Td>
                      <div className="flex gap-1">
                        {u.roles.map((r) => (
                          <Badge key={r} tone="neutral" className="capitalize">
                            {r}
                          </Badge>
                        ))}
                      </div>
                    </Td>
                    <Td>
                      <div className="flex items-center gap-1.5">
                        <Badge tone={u.is_active ? "positive" : "inactive"}>{u.is_active ? "Active" : "Inactive"}</Badge>
                        {u.password_reset_requested && <Badge tone="warning">Reset requested</Badge>}
                      </div>
                    </Td>
                    <Td>
                      <Button size="sm" variant="secondary" onClick={() => setResetTarget(u)}>
                        Reset password
                      </Button>
                    </Td>
                  </tr>
                ))}
              </Tbody>
            </Table>
          )}
        </CardContent>
      </Card>

      <CreateUserModal open={modalOpen} onClose={() => setModalOpen(false)} />
      <ResetPasswordModal user={resetTarget} onClose={() => setResetTarget(null)} />
    </div>
  );
}
