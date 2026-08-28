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
  /** Phase this screen ships real functionality in; undefined = live now. */
  phase?: number;
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
      { label: "Paper Trading", href: "/paper-trading", icon: Activity, phase: 6 },
      { label: "Live Trading", href: "/live-trading", icon: Radio, phase: 7 },
      { label: "Portfolio", href: "/portfolio", icon: Briefcase, phase: 7 },
      { label: "Orders", href: "/orders", icon: ListOrdered, phase: 7 },
      { label: "Positions", href: "/positions", icon: Wallet, phase: 7 },
      { label: "Risk Management", href: "/risk", icon: ShieldAlert, phase: 7 },
    ],
  },
  {
    label: "System",
    items: [
      { label: "Alerts", href: "/alerts", icon: Bell, phase: 8 },
      { label: "Reports", href: "/reports", icon: FileText, phase: 8 },
      { label: "System Monitor", href: "/system-monitor", icon: AlertTriangle, phase: 8 },
      { label: "Settings", href: "/settings", icon: Cog },
    ],
  },
];
