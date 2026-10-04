import { type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { call, client, setReauthHandler } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input, Label } from "@/components/ui/input";
import { ErrorNote, Spinner } from "@/components/ui/misc";

/** Prompts for the password when the server requires recent authentication for a sensitive action. */
export function ReauthProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const resolver = useRef<((ok: boolean) => void) | null>(null);

  useEffect(() => {
    setReauthHandler(
      () =>
        new Promise<boolean>((resolve) => {
          resolver.current = resolve;
          setPassword("");
          setError(null);
          setOpen(true);
        }),
    );
    return () => setReauthHandler(null);
  }, []);

  const finish = (ok: boolean) => {
    resolver.current?.(ok);
    resolver.current = null;
    setOpen(false);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await call(() => client.POST("/api/v1/auth/reauth", { body: { password } }));
      finish(true);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {children}
      <Dialog open={open} onOpenChange={(o) => !o && finish(false)}>
        <DialogContent className="max-w-sm">
          <form onSubmit={submit} className="flex flex-col gap-4">
            <DialogHeader>
              <DialogTitle>Confirm your password</DialogTitle>
              <DialogDescription>This is a sensitive action. Please confirm it's you.</DialogDescription>
            </DialogHeader>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="reauth-password">Password</Label>
              <Input id="reauth-password" type="password" autoComplete="current-password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} required />
            </div>
            <ErrorNote error={error} />
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => finish(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={busy}>
                {busy && <Spinner className="text-primary-foreground" />}
                Confirm
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
