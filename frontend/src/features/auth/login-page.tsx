import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, ShieldCheck } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router";
import { call, client, ensureCsrf } from "@/api/client";
import { keys, type SessionState, useSession } from "@/api/queries";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { toast } from "sonner";
import { describeAgent } from "@/lib/agent";
import { formatDateTime } from "@/lib/utils";
import { AuthShell } from "./auth-shell";
import { cancelWebauthn, getAssertion, webauthnErrorMessage, webauthnSupported } from "./webauthn";

function announcePreviousLogin(state: SessionState) {
  const prev = state.previous_login;
  const failed = state.failed_since_previous_login ?? 0;
  if (failed > 0) {
    toast.warning(`${failed} failed sign-in attempt${failed === 1 ? "" : "s"} since your last visit`, {
      description: "Check Settings → Security → Sign-in activity. Change your password if this wasn't you.",
      duration: 15000,
    });
  }
  if (prev) {
    toast.info("Welcome back", {
      description: `Last sign-in ${formatDateTime(prev.at)} from ${prev.ip ?? "unknown IP"} (${describeAgent(prev.user_agent)})`,
      duration: 8000,
    });
  }
}

function safeNext(next: string | null): string {
  return next && next.startsWith("/") && !next.startsWith("//") ? next : "/inbox";
}

export function LoginPage() {
  const session = useSession();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));

  const done = (state: SessionState) => {
    qc.setQueryData(keys.session, state);
    if (state.mfa_complete) {
      announcePreviousLogin(state);
      navigate(next, { replace: true });
    }
    else if (state.authenticated) navigate("/setup", { replace: true });
  };

  if (session.isPending) return null;
  if (session.data?.mfa_complete) return <Navigate to={next} replace />;
  if (session.data?.authenticated) return <Navigate to="/setup" replace />;

  if (session.data?.pending_mfa) {
    return <MfaStep methods={session.data.pending_mfa} onDone={done} onRestart={() => qc.invalidateQueries({ queryKey: keys.session })} />;
  }
  return <PasswordStep onDone={done} />;
}

function PasswordStep({ onDone }: { onDone: (s: SessionState) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await ensureCsrf();
      onDone(await call(() => client.POST("/api/v1/auth/login", { body: { username, password } })));
    } catch (err) {
      setError(err);
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthShell title="Sign in to DocNest" subtitle="Your private document archive">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="username">Username</Label>
          <Input id="username" autoComplete="username" autoFocus required value={username} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="password">Password</Label>
          <Input id="password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        <ErrorNote error={error} />
        <Button type="submit" disabled={busy}>
          {busy && <Spinner className="text-primary-foreground" />}
          Continue
        </Button>
      </form>
    </AuthShell>
  );
}

function MfaStep({
  methods,
  onDone,
  onRestart,
}: {
  methods: NonNullable<SessionState["pending_mfa"]>;
  onDone: (s: SessionState) => void;
  onRestart: () => void;
}) {
  const [mode, setModeState] = useState<"webauthn" | "totp" | "recovery">(
    methods.webauthn && webauthnSupported() ? "webauthn" : methods.totp ? "totp" : "recovery",
  );
  const [code, setCode] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const setMode = (next: "webauthn" | "totp" | "recovery") => {
    if (next !== "webauthn") cancelWebauthn();
    setBusy(false);
    setError(null);
    setModeState(next);
  };

  const run = async (fn: () => Promise<SessionState>) => {
    setBusy(true);
    setError(null);
    try {
      onDone(await fn());
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") return; // user switched method
      if (err instanceof Error && "status" in err && (err as { status: number }).status === 401) onRestart();
      setError(err instanceof Error && err.name !== "ApiError" && mode === "webauthn" ? new Error(webauthnErrorMessage(err)) : err);
      setCode("");
    } finally {
      setBusy(false);
    }
  };

  const signInWithKey = () =>
    run(async () => {
      const options = await call(() => client.POST("/api/v1/auth/mfa/webauthn/options"));
      const credential = await getAssertion(options);
      return call(() =>
        client.POST("/api/v1/auth/mfa/webauthn/verify", { body: { name: "", credential: credential as unknown as Record<string, unknown> } }),
      );
    });

  useEffect(() => {
    if (mode === "webauthn") void signInWithKey();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submitCode = (e: FormEvent) => {
    e.preventDefault();
    run(() =>
      call(() =>
        mode === "totp"
          ? client.POST("/api/v1/auth/mfa/totp", { body: { code } })
          : client.POST("/api/v1/auth/mfa/recovery", { body: { code } }),
      ),
    );
  };

  return (
    <AuthShell title="Confirm it's you" subtitle="A second factor is required">
      <div className="flex flex-col gap-4">
        {mode === "webauthn" ? (
          <div className="flex flex-col items-center gap-3 py-2 text-center">
            <KeyRound className="size-10 text-primary" />
            <p className="text-sm text-muted-foreground">Use your passkey or security key to continue.</p>
            <Button className="w-full" onClick={signInWithKey} disabled={busy}>
              {busy && <Spinner className="text-primary-foreground" />}
              Use security key
            </Button>
          </div>
        ) : (
          <form onSubmit={submitCode} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="code">{mode === "totp" ? "6-digit code from your authenticator app" : "Recovery code"}</Label>
              <Input
                id="code"
                autoFocus
                autoComplete="one-time-code"
                inputMode={mode === "totp" ? "numeric" : "text"}
                placeholder={mode === "totp" ? "123456" : "xxxx-xxxx-xxxx"}
                value={code}
                onChange={(e) => setCode(e.target.value)}
                required
              />
            </div>
            <Button type="submit" disabled={busy}>
              {busy && <Spinner className="text-primary-foreground" />}
              <ShieldCheck /> Verify
            </Button>
          </form>
        )}
        <ErrorNote error={error} />
        <div className="flex flex-col gap-1 border-t pt-3 text-sm">
          {mode !== "webauthn" && methods.webauthn && webauthnSupported() && (
            <button type="button" className="text-left text-primary hover:underline cursor-pointer" onClick={() => setMode("webauthn")}>
              Use a security key instead
            </button>
          )}
          {mode !== "totp" && methods.totp && (
            <button type="button" className="text-left text-primary hover:underline cursor-pointer" onClick={() => setMode("totp")}>
              Use authenticator app
            </button>
          )}
          {mode !== "recovery" && methods.recovery && (
            <button type="button" className="text-left text-primary hover:underline cursor-pointer" onClick={() => setMode("recovery")}>
              Use a recovery code
            </button>
          )}
        </div>
      </div>
    </AuthShell>
  );
}
