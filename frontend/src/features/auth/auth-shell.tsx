import type { ReactNode } from "react";

export function AuthShell({
  title,
  subtitle,
  wide = false,
  children,
}: {
  title: string;
  subtitle?: string;
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-full items-center justify-center bg-muted/40 px-4 py-10">
      <div className={wide ? "w-full max-w-lg" : "w-full max-w-sm"}>
        <div className="mb-6 flex flex-col items-center text-center">
          <img src="/favicon.svg" alt="" className="mb-3 size-11" />
          <h1 className="text-xl font-semibold">{title}</h1>
          {subtitle && <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>}
        </div>
        <div className="rounded-lg border bg-card p-6 shadow-sm">{children}</div>
        <p className="mt-6 text-center text-xs text-muted-foreground">DocNest · private document archive</p>
      </div>
    </div>
  );
}
