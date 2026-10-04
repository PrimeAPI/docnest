import { cva, type VariantProps } from "class-variance-authority";
import type * as React from "react";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap [&_svg]:size-3",
  {
    variants: {
      variant: {
        default: "bg-accent text-accent-foreground",
        outline: "border text-muted-foreground",
        success: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
        warning: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
        danger: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-200",
        muted: "bg-muted text-muted-foreground",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export function Badge({
  className,
  variant,
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
