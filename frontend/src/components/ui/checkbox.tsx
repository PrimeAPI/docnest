import * as CB from "@radix-ui/react-checkbox";
import { Check } from "lucide-react";
import type * as React from "react";
import { cn } from "@/lib/utils";

export function Checkbox({ className, ...props }: React.ComponentProps<typeof CB.Root>) {
  return (
    <CB.Root
      className={cn(
        "peer size-4 shrink-0 rounded-sm border border-input shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring data-[state=checked]:border-primary data-[state=checked]:bg-primary data-[state=checked]:text-primary-foreground cursor-pointer",
        className,
      )}
      {...props}
    >
      <CB.Indicator className="flex items-center justify-center">
        <Check className="size-3" strokeWidth={3} />
      </CB.Indicator>
    </CB.Root>
  );
}
