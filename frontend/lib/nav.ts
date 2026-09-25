import type { LucideIcon } from "lucide-react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bell,
  Bot,
  Briefcase,
  Cog,
  FileText,
  Filter,
  Gauge,
  LayoutDashboard,
  LineChart,
  ListOrdered,
  Layers,
  Radio,
  ShieldAlert,
  TrendingUp,
  Wallet,
  Wrench,
} from "lucide-react";

export interface NavItem {
  label: string;
  href: string;
  icon: LucideIcon;
  /** True if this screen is still a scaffolded placeholder, not real yet. */
  unbuilt?: boolean;
}

export const NAV_SECTIONS: { label: string; items: NavItem[] }[] = [
  {
    label: "Overview",
    items: [{ label: "Dashboard", href: "/dashboard", icon: LayoutDashboard }],
  },
  {
    label: "Research",
    items: [
      { label: "Markets", href: "/markets", icon: Radio },
      { label: "Charts", href: "/charts", icon: LineChart },
      { label: "Options", href: "/options", icon: Layers },
      { label: "Scanner", href: "/scanner", icon: Filter },
      { label: "Market Data", href: "/market-data", icon: BarChart3 },
    ],
  },
  {
    label: "Strategies",
    items: [
      { label: "Strategies", href: "/strategies", icon: Bot },
      { label: "Strategy Builder", href: "/strategy-builder", icon: Wrench },
      { label: "Backtesting", href: "/backtesting", icon: TrendingUp },
      { label: "Optimization", href: "/optimization", icon: Gauge },
    ],
  },
  {
    label: "Trading",
    items: [
      { label: "Paper Trading", href: "/paper-trading", icon: Activity },
      { label: "Live Trading", href: "/live-trading", icon: Radio },
      { label: "Portfolio", href: "/portfolio", icon: Briefcase },
      { label: "Orders", href: "/orders", icon: ListOrdered },
      { label: "Positions", href: "/positions", icon: Wallet },
      { label: "Risk Management", href: "/risk", icon: ShieldAlert },
    ],
  },
  {
    label: "System",
    items: [
      { label: "Alerts", href: "/alerts", icon: Bell },
      { label: "Reports", href: "/reports", icon: FileText },
      { label: "System Monitor", href: "/system-monitor", icon: AlertTriangle },
      { label: "Settings", href: "/settings", icon: Cog },
    ],
  },
];

/** The phone/tablet bottom bar (below lg): the most-used pages, one tap
 * away. Every other page is under its "More" sheet. Live Trading is left
 * off on purpose -- real money stays one step further away. */
export const BOTTOM_TABS: { label: string; href: string }[] = [
  { label: "Home", href: "/dashboard" },
  { label: "Paper", href: "/paper-trading" },
  { label: "Charts", href: "/charts" },
  { label: "Data", href: "/market-data" },
];

export function isActivePath(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(href + "/");
}
