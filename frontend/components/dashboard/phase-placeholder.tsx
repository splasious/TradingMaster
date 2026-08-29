import { Construction } from "lucide-react";

export function PhasePlaceholder({ title, description }: { title: string; description?: string }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border p-16 text-center">
      <Construction className="h-8 w-8 text-text-muted" />
      <h2 className="text-lg font-semibold text-text-primary">{title}</h2>
      <p className="max-w-sm text-sm text-text-secondary">
        {description ?? "This module is scaffolded and routed, but not yet built -- a consolidated cross-environment view is a later polish pass."}
      </p>
    </div>
  );
}
