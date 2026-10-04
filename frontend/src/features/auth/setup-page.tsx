import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, Smartphone } from "lucide-react";
import { useState } from "react";
import { Navigate, useNavigate } from "react-router";
import { call, client } from "@/api/client";
import { keys, type SessionState, useSession } from "@/api/queries";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AuthShell } from "./auth-shell";
import { KeyEnroll, RecoveryCodes, TotpEnroll } from "./enroll";
import { webauthnSupported } from "./webauthn";

export function SetupPage() {
  const session = useSession();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [codes, setCodes] = useState<string[] | null>(null);

  if (session.isPending) return null;
  if (!session.data?.authenticated && !codes) return <Navigate to="/login" replace />;
  if (session.data?.mfa_complete && !codes) return <Navigate to="/inbox" replace />;

  const enrolled = async (state: SessionState) => {
    const result = await call(() => client.POST("/api/v1/account/recovery-codes"));
    setCodes(result.codes);
    qc.setQueryData(keys.session, state);
  };

  if (codes) {
    return (
      <AuthShell title="Save your recovery codes" subtitle="Your second factor is set up">
        <RecoveryCodes codes={codes} />
        <Button className="mt-5 w-full" onClick={() => navigate("/inbox", { replace: true })}>
          I have saved them — continue
        </Button>
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Protect your account" subtitle="Set up a second factor to continue">
      <Tabs defaultValue={webauthnSupported() ? "key" : "totp"}>
        <TabsList className="mb-2 grid w-full grid-cols-2">
          <TabsTrigger value="key">
            <KeyRound /> Security key
          </TabsTrigger>
          <TabsTrigger value="totp">
            <Smartphone /> Authenticator
          </TabsTrigger>
        </TabsList>
        <TabsContent value="key">
          <KeyEnroll onDone={enrolled} />
        </TabsContent>
        <TabsContent value="totp">
          <TotpEnroll onDone={enrolled} />
        </TabsContent>
      </Tabs>
    </AuthShell>
  );
}
