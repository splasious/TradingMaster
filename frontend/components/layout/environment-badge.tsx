import { Badge } from "@/components/ui/badge";

/** The active environment must always be visible (PRD section 6). Paper and
 * Live trading modes are introduced in later phases; until then this shows
 * the deployment environment so it's never ambiguous whether you're looking
 * at development or a real deployment. */
export function EnvironmentBadge() {
  const env = process.env.NEXT_PUBLIC_ENVIRONMENT ?? "development";
  const tone = env === "production" ? "critical" : env === "staging" ? "warning" : "neutral";
  return <Badge tone={tone}>{env.toUpperCase()}</Badge>;
}
