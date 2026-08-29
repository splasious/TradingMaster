"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { DatabaseBackup, Download } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiDownload, apiFetch, ApiError } from "@/lib/api";
import { useBackups } from "@/lib/hooks";
import type { BackupOut } from "@/lib/types";

function formatSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export default function BackupSettingsPage() {
  const { data: backups, isLoading } = useBackups();
  const queryClient = useQueryClient();

  const createMutation = useMutation({
    mutationFn: () => apiFetch<BackupOut>("/api/v1/backup", { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["backups"] }),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Backup &amp; Restore</h1>
          <p className="text-sm text-text-muted">
            SQLite: a consistent file-copy snapshot, taken via sqlite3&apos;s own backup API. PostgreSQL: use pg_dump/pg_restore
            directly against the database.
          </p>
        </div>
        <Button onClick={() => createMutation.mutate()} disabled={createMutation.isPending}>
          <DatabaseBackup className="h-3.5 w-3.5" /> {createMutation.isPending ? "Backing up..." : "Create Backup"}
        </Button>
      </div>

      {createMutation.isError && (
        <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">
          {createMutation.error instanceof ApiError ? createMutation.error.message : "Backup failed"}
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Backups</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <p className="p-5 text-sm text-text-muted">Loading...</p>
          ) : !backups?.length ? (
            <p className="p-5 text-sm text-text-muted">No backups yet.</p>
          ) : (
            <Table>
              <Thead>
                <tr>
                  <Th>Filename</Th>
                  <Th>Size</Th>
                  <Th>Created</Th>
                  <Th />
                </tr>
              </Thead>
              <Tbody>
                {backups.map((b) => (
                  <tr key={b.filename}>
                    <Td className="font-mono text-xs">{b.filename}</Td>
                    <Td>{formatSize(b.size_bytes)}</Td>
                    <Td className="text-xs text-text-muted">{new Date(b.created_at).toLocaleString()}</Td>
                    <Td className="text-right">
                      <Button variant="ghost" size="sm" onClick={() => apiDownload(`/api/v1/backup/${b.filename}/download`, b.filename)}>
                        <Download className="h-3.5 w-3.5" /> Download
                      </Button>
                    </Td>
                  </tr>
                ))}
              </Tbody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
