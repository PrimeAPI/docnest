import QRCode from "qrcode";
import { Copy, Download, KeyRound, Smartphone } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";
import { call, client } from "@/api/client";
import type { SessionState } from "@/api/queries";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { createCredential, webauthnErrorMessage, webauthnSupported } from "./webauthn";

export function KeyEnroll({ onDone }: { onDone: (s: SessionState) => void }) {
  const [name, setName] = useState("Security key");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const register = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const options = await call(() => client.POST("/api/v1/account/webauthn/register/options"));
      let credential;
      try {
        credential = await createCredential(options);
      } catch (err) {
        throw new Error(webauthnErrorMessage(err));
      }
      onDone(
        await call(() =>
          client.POST("/api/v1/account/webauthn/register/verify", {
            body: { name, credential: credential as unknown as Record<string, never> },
          }),
        ),
      );
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  if (!webauthnSupported()) {
    return <p className="text-sm text-muted-foreground">This browser does not support security keys or passkeys.</p>;
  }
  return (
    <form onSubmit={register} className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">
        Passkeys and hardware security keys (e.g. YubiKey) are the most secure option and protect against phishing.
      </p>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="keyname">Name</Label>
        <Input id="keyname" value={name} maxLength={100} onChange={(e) => setName(e.target.value)} required />
      </div>
      <ErrorNote error={error} />
      <Button type="submit" disabled={busy}>
        {busy ? <Spinner className="text-primary-foreground" /> : <KeyRound />}
        Register security key
      </Button>
    </form>
  );
}

export function TotpEnroll({ onDone }: { onDone: (s: SessionState) => void }) {
  const [setup, setSetup] = useState<{ secret: string; uri: string } | null>(null);
  const [qr, setQr] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    let active = true;
    call(() => client.POST("/api/v1/account/totp/setup"))
      .then(async (s) => {
        if (!active) return;
        setSetup(s);
        setQr(await QRCode.toDataURL(s.uri, { margin: 1, width: 200 }));
      })
      .catch(setError);
    return () => {
      active = false;
    };
  }, []);

  const confirm = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onDone(await call(() => client.POST("/api/v1/account/totp/confirm", { body: { code } })));
    } catch (err) {
      setError(err);
      setCode("");
    } finally {
      setBusy(false);
    }
  };

  if (!setup) return error ? <ErrorNote error={error} /> : <Spinner />;
  return (
    <form onSubmit={confirm} className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">
        Scan the QR code with an authenticator app (e.g. Aegis, 2FAS, Proton Pass), then enter the 6-digit code.
      </p>
      {qr && <img src={qr} alt="TOTP QR code" className="mx-auto size-48 rounded-md border bg-white p-1" />}
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer">Can't scan? Enter the key manually</summary>
        <code className="mt-2 block break-all rounded bg-muted p-2 font-mono">{setup.secret}</code>
      </details>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="totp">Verification code</Label>
        <Input id="totp" inputMode="numeric" autoComplete="one-time-code" placeholder="123456" value={code} onChange={(e) => setCode(e.target.value)} required />
      </div>
      <ErrorNote error={error} />
      <Button type="submit" disabled={busy}>
        {busy ? <Spinner className="text-primary-foreground" /> : <Smartphone />}
        Confirm
      </Button>
    </form>
  );
}

export function RecoveryCodes({ codes }: { codes: string[] }) {
  const text = `DocNest recovery codes\nEach code can be used once.\n\n${codes.join("\n")}\n`;
  const download = () => {
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "docnest-recovery-codes.txt";
    a.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-2 rounded-md bg-muted p-3 font-mono text-sm">
        {codes.map((c) => (
          <span key={c}>{c}</span>
        ))}
      </div>
      <div className="flex gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => navigator.clipboard.writeText(text).then(() => toast.success("Copied"))}
        >
          <Copy /> Copy
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={download}>
          <Download /> Download
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Store these codes somewhere safe (e.g. your password manager). Each one lets you sign in once if you lose your
        second factor.
      </p>
    </div>
  );
}
