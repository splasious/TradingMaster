import { AlertCircle, Inbox, Loader2, type LucideIcon } from "lucide-react";

interface DataStateProps {
  title: string;
  description?: string;
  icon?: LucideIcon;
  action?: React.ReactNode;
}

/** Shared visual treatment for the loading/empty/error states every
 * data-dependent panel needs (PRD 39.3 / UI spec section 66) -- one
 * component instead of a bespoke "No X yet" string per page. */
function DataStateBase({ title, description, icon: Icon = Inbox, action, className }: DataStateProps & { className: string }) {
  return (
    <div className={`flex flex-col items-center justify-center gap-2 p-10 text-center ${className}`}>
      <Icon className="h-6 w-6" />
      <p className="text-sm font-medium text-text-primary">{title}</p>
      {description && <p className="max-w-sm text-sm text-text-muted">{description}</p>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}

export function LoadingState({ title = "Loading...", description }: Partial<DataStateProps>) {
  return <DataStateBase title={title} description={description} icon={Loader2} className="text-text-muted [&>svg]:animate-spin" />;
}

export function EmptyState({ title, description, icon = Inbox, action }: DataStateProps) {
  return <DataStateBase title={title} description={description} icon={icon} action={action} className="text-text-muted" />;
}

export function ErrorState({ title = "Something went wrong", description, icon = AlertCircle }: Partial<DataStateProps>) {
  return <DataStateBase title={title} description={description} icon={icon} className="text-negative" />;
}
