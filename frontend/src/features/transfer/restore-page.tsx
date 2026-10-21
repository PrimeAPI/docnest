import { Navigate } from "react-router";
import { AuthShell } from "@/features/auth/auth-shell";
import { ImportFlow, ImportProgress } from "./import-flow";
import { useTransferStatus } from "./transfer-api";

/** A new instance without users: restore an export before anyone signs in. */
export function RestorePage() {
  const status = useTransferStatus();
  const s = status.data;
  if (status.isPending) return null;
  const importing = s?.operation === "import" && (s.state === "running" || s.state === "done");
  if (!importing && !s?.instance_empty) return <Navigate to="/login" replace />;

  return (
    <AuthShell title="Restore an export" subtitle="Move an existing DocNest to this server" wide>
      {importing ? <ImportProgress /> : <ImportFlow needsSetupCode />}
    </AuthShell>
  );
}
