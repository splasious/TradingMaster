import Link from "next/link";
import { ChevronRight, Plug, Users } from "lucide-react";

import { Card } from "@/components/ui/card";

const SETTINGS_LINKS = [
  { href: "/settings/brokers", label: "Brokers", description: "Connect and manage broker accounts", icon: Plug },
  { href: "/settings/users", label: "Users", description: "Manage users and role assignments", icon: Users },
];

export default function SettingsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-text-primary">Settings</h1>
      <div className="grid max-w-2xl grid-cols-1 gap-3">
        {SETTINGS_LINKS.map(({ href, label, description, icon: Icon }) => (
          <Link key={href} href={href}>
            <Card className="flex items-center justify-between p-4 transition-colors hover:border-border-strong">
              <div className="flex items-center gap-3">
                <Icon className="h-4 w-4 text-text-secondary" />
                <div>
                  <div className="text-sm font-medium text-text-primary">{label}</div>
                  <div className="text-xs text-text-muted">{description}</div>
                </div>
              </div>
              <ChevronRight className="h-4 w-4 text-text-muted" />
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
