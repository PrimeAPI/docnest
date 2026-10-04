import * as DM from "@radix-ui/react-dropdown-menu";
import type * as React from "react";
import { cn } from "@/lib/utils";

export const DropdownMenu = DM.Root;
export const DropdownMenuTrigger = DM.Trigger;

export function DropdownMenuContent({ className, ...props }: React.ComponentProps<typeof DM.Content>) {
  return (
    <DM.Portal>
      <DM.Content
        sideOffset={4}
        align="end"
        className={cn("z-50 min-w-44 rounded-md border bg-card p-1 text-sm shadow-lg", className)}
        {...props}
      />
    </DM.Portal>
  );
}

export function DropdownMenuItem({ className, ...props }: React.ComponentProps<typeof DM.Item>) {
  return (
    <DM.Item
      className={cn(
        "flex cursor-pointer select-none items-center gap-2 rounded-sm px-2 py-1.5 outline-none data-[highlighted]:bg-muted data-[disabled]:opacity-50 [&_svg]:size-4",
        className,
      )}
      {...props}
    />
  );
}

export function DropdownMenuSeparator() {
  return <DM.Separator className="my-1 h-px bg-border" />;
}
