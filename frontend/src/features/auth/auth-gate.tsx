import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useEffect } from "react";
import { Navigate, useLocation } from "react-router";
import { setUnauthorizedHandler } from "@/api/client";
import { keys, useSession } from "@/api/queries";
import { Spinner } from "@/components/ui/misc";
import { ReauthProvider } from "./reauth-dialog";

export function AuthGate({ children }: { children: ReactNode }) {
  const session = useSession();
  const location = useLocation();
  const qc = useQueryClient();

  useEffect(() => {
    setUnauthorizedHandler(() => qc.invalidateQueries({ queryKey: keys.session }));
    return () => setUnauthorizedHandler(null);
  }, [qc]);

  if (session.isPending) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="size-6" />
      </div>
    );
  }
  const s = session.data;
  if (!s || !s.authenticated) {
    const next = location.pathname + location.search;
    return <Navigate to={`/login${next && next !== "/" ? `?next=${encodeURIComponent(next)}` : ""}`} replace />;
  }
  if (!s.mfa_complete) return <Navigate to="/setup" replace />;
  return <ReauthProvider>{children}</ReauthProvider>;
}
