import { type ButtonHTMLAttributes, forwardRef } from "react";

import { cn } from "@/lib/utils";

const variants = {
  primary: "bg-brand text-brand-foreground hover:opacity-90",
  secondary: "bg-surface-elevated text-text-primary border border-border hover:border-border-strong",
  ghost: "text-text-secondary hover:bg-surface-elevated hover:text-text-primary",
  destructive: "bg-negative text-white hover:opacity-90",
} as const;

const sizes = {
  sm: "h-8 px-3 text-sm",
  md: "h-9 px-4 text-sm",
  lg: "h-11 px-6 text-base",
} as const;

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: keyof typeof variants;
  size?: keyof typeof sizes;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "primary", size = "md", ...props }, ref) => {
    return (
      <button
        ref={ref}
        className={cn(
          "inline-flex items-center justify-center gap-2 rounded-md font-medium transition-colors disabled:opacity-50 disabled:pointer-events-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand focus-visible:ring-offset-2 focus-visible:ring-offset-background",
          variants[variant],
          sizes[size],
          className,
        )}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";
