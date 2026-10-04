import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Copy,
  HardDrive,
  KeyRound,
  Monitor,
  Plus,
  RotateCw,
  ScanLine,
  Shield,
  Smartphone,
  Trash2,
} from "lucide-react";
import { type FormEvent, useState } from "react";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { keys, useSystem } from "@/api/queries";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input, Label } from "@/components/ui/input";
import { EmptyState, ErrorNote, PageHeader, Spinner } from "@/components/ui/misc";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { KeyEnroll, RecoveryCodes, TotpEnroll } from "@/features/auth/enroll";
import { formatDateTime, relativeTime } from "@/lib/utils";

export function SettingsPage() {
  return (
    <>
      <PageHeader title="Settings" />
      <Tabs defaultValue="security">
        <TabsList className="flex-wrap">
          <TabsTrigger value="security">
            <Shield /> Security
          </TabsTrigger>
          <TabsTrigger value="scanners">
            <ScanLine /> Scanners
          </TabsTrigger>
          <TabsTrigger value="system">
            <Activity /> System
          </TabsTrigger>
        </TabsList>
        <TabsContent value="security" className="flex flex-col gap-6">
          <SecondFactors />
          <Sessions />
          <PasswordCard />
        </TabsContent>
        <TabsContent value="scanners">
          <Scanners />
        </TabsContent>
        <TabsContent value="system" className="flex flex-col gap-6">
          <SystemCard />
          <AuditLog />
        </TabsContent>
      </Tabs>
    </>
  );
}

// --- Security -------------------------------------------------------------------

function SecondFactors() {
  const qc = useQueryClient();
  const devices = useQuery({ queryKey: keys.devices, queryFn: () => call(() => client.GET("/api/v1/account/devices")) });
  const [adding, setAdding] = useState<"key" | "totp" | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const remove = useMutation({
    mutationFn: (d: Schemas["DeviceOut"]) =>
      call(() =>
        client.DELETE("/api/v1/account/devices/{kind}/{device_id}", {
          params: { path: { kind: d.kind, device_id: d.id } },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.devices }),
    onError: (e) => toast.error(e.message),
  });
  const regenerate = async () => {
    try {
      const r = await call(() => client.POST("/api/v1/account/recovery-codes"));
      setCodes(r.codes);
    } catch (e) {
      toast.error((e as Error).message);
    }
  };
  const added = () => {
    setAdding(null);
    qc.invalidateQueries({ queryKey: keys.devices });
    toast.success("Second factor added");
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Two-factor authentication</CardTitle>
        <CardDescription>Register more than one factor so you never lock yourself out.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {devices.isPending ? (
          <Spinner />
        ) : (
          <div className="divide-y rounded-md border">
            {devices.data?.map((d) => (
              <div key={`${d.kind}-${d.id}`} className="flex items-center gap-3 px-3 py-2.5 text-sm">
                {d.kind === "webauthn" ? <KeyRound className="size-4" /> : <Smartphone className="size-4" />}
                <div className="min-w-0 flex-1">
                  <div className="font-medium">{d.name}</div>
                  <div className="text-xs text-muted-foreground">
                    {d.kind === "webauthn" ? "Security key / passkey" : "Authenticator app"} · added{" "}
                    {relativeTime(d.created_at)}
                    {d.last_used_at && ` · used ${relativeTime(d.last_used_at)}`}
                  </div>
                </div>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  title="Remove"
                  onClick={() => confirm(`Remove “${d.name}”?`) && remove.mutate(d)}
                >
                  <Trash2 />
                </Button>
              </div>
            ))}
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => setAdding("key")}>
            <KeyRound /> Add security key
          </Button>
          <Button variant="outline" onClick={() => setAdding("totp")}>
            <Smartphone /> Add authenticator app
          </Button>
          <Button variant="outline" onClick={regenerate}>
            <RotateCw /> New recovery codes
          </Button>
        </div>
      </CardContent>
      <Dialog open={adding !== null} onOpenChange={(o) => !o && setAdding(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{adding === "key" ? "Add security key" : "Add authenticator app"}</DialogTitle>
          </DialogHeader>
          {adding === "key" && <KeyEnroll onDone={added} />}
          {adding === "totp" && <TotpEnroll onDone={added} />}
        </DialogContent>
      </Dialog>
      <Dialog open={codes !== null} onOpenChange={(o) => !o && setCodes(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New recovery codes</DialogTitle>
            <DialogDescription>Your previous codes no longer work.</DialogDescription>
          </DialogHeader>
          {codes && <RecoveryCodes codes={codes} />}
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function Sessions() {
  const qc = useQueryClient();
  const sessions = useQuery({ queryKey: keys.sessions, queryFn: () => call(() => client.GET("/api/v1/account/sessions")) });
  const revokeOthers = useMutation({
    mutationFn: () => call(() => client.POST("/api/v1/account/sessions/revoke-others")),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: keys.sessions });
      toast.success(`Signed out ${r.revoked} other session(s)`);
    },
  });
  const revoke = useMutation({
    mutationFn: (id: number) =>
      call(() => client.DELETE("/api/v1/account/sessions/{session_id}", { params: { path: { session_id: id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.sessions }),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Active sessions</CardTitle>
        <CardDescription>Sessions end after 30 minutes of inactivity and after 12 hours at most.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="divide-y rounded-md border">
          {sessions.data?.map((s) => (
            <div key={s.id} className="flex items-center gap-3 px-3 py-2.5 text-sm">
              <Monitor className="size-4 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium">{s.user_agent || "Unknown browser"}</div>
                <div className="text-xs text-muted-foreground">
                  {s.ip ?? "unknown IP"} · signed in {relativeTime(s.created_at)} · active{" "}
                  {relativeTime(s.last_seen_at)}
                </div>
              </div>
              {s.current ? (
                <Badge variant="success">This device</Badge>
              ) : (
                <Button size="sm" variant="ghost" onClick={() => revoke.mutate(s.id)}>
                  Sign out
                </Button>
              )}
            </div>
          ))}
        </div>
        <Button variant="outline" className="self-start" onClick={() => revokeOthers.mutate()}>
          Sign out all other sessions
        </Button>
      </CardContent>
    </Card>
  );
}

function PasswordCard() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<unknown>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (next !== repeat) return setError(new Error("The new passwords do not match"));
    try {
      await call(() =>
        client.POST("/api/v1/account/password", { body: { current_password: current, new_password: next } }),
      );
      toast.success("Password changed. Other sessions were signed out.");
      setCurrent("");
      setNext("");
      setRepeat("");
    } catch (err) {
      setError(err);
    }
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Password</CardTitle>
        <CardDescription>At least 12 characters with three character classes, or a passphrase of 20+ characters.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="grid max-w-md gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="pw-current">Current password</Label>
            <Input id="pw-current" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="pw-new">New password</Label>
            <Input id="pw-new" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="pw-repeat">Repeat new password</Label>
            <Input id="pw-repeat" type="password" autoComplete="new-password" value={repeat} onChange={(e) => setRepeat(e.target.value)} required />
          </div>
          <ErrorNote error={error} />
          <Button type="submit" className="justify-self-start">
            Change password
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

// --- Scanners -------------------------------------------------------------------

function Scanners() {
  const qc = useQueryClient();
  const scanners = useQuery({ queryKey: keys.scanners, queryFn: () => call(() => client.GET("/api/v1/scanners")) });
  const [creating, setCreating] = useState(false);
  const [token, setToken] = useState<string | null>(null);
  const invalidate = () => qc.invalidateQueries({ queryKey: keys.scanners });
  const action = useMutation({
    mutationFn: async ({ id, kind }: { id: number; kind: "rotate" | "revoke" | "delete" }) => {
      if (kind === "rotate") {
        const r = await call(() => client.POST("/api/v1/scanners/{scanner_id}/rotate", { params: { path: { scanner_id: id } } }));
        setToken(r.token);
      } else if (kind === "revoke") {
        await call(() => client.POST("/api/v1/scanners/{scanner_id}/revoke", { params: { path: { scanner_id: id } } }));
      } else {
        await call(() => client.DELETE("/api/v1/scanners/{scanner_id}", { params: { path: { scanner_id: id } } }));
      }
    },
    onSuccess: invalidate,
    onError: (e) => toast.error(e.message),
  });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="max-w-2xl text-sm text-muted-foreground">
          Scanners upload documents with their own token. A scanner token can only upload and check upload status — it
          can never read, search or download your archive.
        </p>
        <Button onClick={() => setCreating(true)}>
          <Plus /> Add scanner
        </Button>
      </div>
      {scanners.isPending ? (
        <Spinner />
      ) : !scanners.data?.length ? (
        <EmptyState icon={<ScanLine />} title="No scanners yet">
          Add a scanner to get an upload token for your device.
        </EmptyState>
      ) : (
        <Card className="divide-y">
          {scanners.data.map((s) => (
            <div key={s.id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
              <ScanLine className="size-4" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 font-medium">
                  {s.name}
                  {s.active ? <Badge variant="success">Active</Badge> : <Badge variant="danger">Revoked</Badge>}
                </div>
                <div className="text-xs text-muted-foreground">
                  <code>{s.token_prefix}</code> · {s.document_count} uploads · last used{" "}
                  {s.last_used_at ? `${relativeTime(s.last_used_at)} from ${s.last_used_ip}` : "never"}
                  {s.allowed_ips.length > 0 && ` · only from ${s.allowed_ips.join(", ")}`}
                </div>
              </div>
              <Button size="sm" variant="outline" onClick={() => action.mutate({ id: s.id, kind: "rotate" })}>
                <RotateCw /> New token
              </Button>
              {s.active && (
                <Button size="sm" variant="outline" onClick={() => action.mutate({ id: s.id, kind: "revoke" })}>
                  Revoke
                </Button>
              )}
              <Button
                size="icon-sm"
                variant="ghost"
                title="Delete"
                onClick={() => confirm(`Delete scanner “${s.name}”?`) && action.mutate({ id: s.id, kind: "delete" })}
              >
                <Trash2 />
              </Button>
            </div>
          ))}
        </Card>
      )}
      <CreateScannerDialog
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(t) => {
          setCreating(false);
          setToken(t);
          invalidate();
        }}
      />
      <TokenDialog token={token} onClose={() => setToken(null)} />
    </div>
  );
}

function CreateScannerDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (token: string) => void;
}) {
  const [name, setName] = useState("");
  const [ips, setIps] = useState("");
  const [error, setError] = useState<unknown>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const r = await call(() =>
        client.POST("/api/v1/scanners", {
          body: {
            name,
            allow_status: true,
            allowed_ips: ips
              .split(",")
              .map((x) => x.trim())
              .filter(Boolean),
          },
        }),
      );
      setName("");
      setIps("");
      onCreated(r.token);
    } catch (err) {
      setError(err);
    }
  };
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>Add scanner</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="scanner-name">Name</Label>
            <Input id="scanner-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Office scanner" required />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="scanner-ips">Allowed IPs / networks (optional)</Label>
            <Input id="scanner-ips" value={ips} onChange={(e) => setIps(e.target.value)} placeholder="192.168.1.0/24" />
            <p className="text-xs text-muted-foreground">
              Restrict uploads to these addresses as seen by DocNest (behind your reverse proxy).
            </p>
          </div>
          <ErrorNote error={error} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit">Create</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function TokenDialog({ token, onClose }: { token: string | null; onClose: () => void }) {
  const origin = window.location.origin;
  const example = `curl -X POST ${origin}/api/upload/v1/documents \\
  -H "Authorization: Bearer ${token}" \\
  -H "Idempotency-Key: $(uuidgen)" \\
  -F "file=@scan.pdf" \\
  -F "bucket=private" \\
  -F "document_type=auto" \\
  -F "todo=false" -F "important=false"`;
  return (
    <Dialog open={token !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Scanner token</DialogTitle>
          <DialogDescription>Copy it now — it is shown only once and stored only as a hash.</DialogDescription>
        </DialogHeader>
        <div className="flex gap-2">
          <Input readOnly value={token ?? ""} className="font-mono text-xs" onFocus={(e) => e.target.select()} />
          <Button variant="outline" onClick={() => navigator.clipboard.writeText(token ?? "").then(() => toast.success("Copied"))}>
            <Copy /> Copy
          </Button>
        </div>
        <div>
          <Label className="text-xs text-muted-foreground">Example upload</Label>
          <pre className="mt-1 overflow-x-auto rounded-md bg-muted p-3 font-mono text-xs">{example}</pre>
        </div>
        <DialogFooter>
          <Button onClick={onClose}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// --- System -----------------------------------------------------------------------

function SystemCard() {
  const system = useSystem();
  const s = system.data;
  if (!s) return <Spinner />;
  return (
    <Card>
      <CardHeader>
        <CardTitle>System status</CardTitle>
        <CardDescription>Version {s.version}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3 sm:grid-cols-3">
        <div className="rounded-md border p-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <HardDrive className="size-4" /> Storage ({s.storage.backend === "proton" ? "Proton Drive" : "local"})
          </div>
          <div className="mt-2">
            {s.storage.ok === null || s.storage.ok === undefined ? (
              <Badge variant="muted">Not checked yet</Badge>
            ) : s.storage.ok ? (
              <Badge variant="success">Connected</Badge>
            ) : (
              <Badge variant="danger">{s.storage.needs_reauth ? "Login required" : "Error"}</Badge>
            )}
          </div>
          {s.storage.message && <p className="mt-2 text-xs text-muted-foreground">{s.storage.message}</p>}
          {s.storage.checked_at && (
            <p className="mt-1 text-xs text-muted-foreground">checked {relativeTime(s.storage.checked_at)}</p>
          )}
        </div>
        <div className="rounded-md border p-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Activity className="size-4" /> Worker
          </div>
          <div className="mt-2">
            {s.worker_online ? <Badge variant="success">Running</Badge> : <Badge variant="danger">Offline</Badge>}
          </div>
          {s.worker_last_seen && (
            <p className="mt-2 text-xs text-muted-foreground">last heartbeat {relativeTime(s.worker_last_seen)}</p>
          )}
        </div>
        <div className="rounded-md border p-3">
          <div className="text-sm font-medium">Jobs</div>
          <div className="mt-2 text-sm">
            {s.queued_jobs} queued · {s.failed_jobs} failed
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function AuditLog() {
  const audit = useQuery({ queryKey: keys.audit, queryFn: () => call(() => client.GET("/api/v1/audit")) });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Security log</CardTitle>
        <CardDescription>Sign-ins, security changes, uploads, downloads and deletions.</CardDescription>
      </CardHeader>
      <CardContent>
        {audit.isPending ? (
          <Spinner />
        ) : (
          <div className="max-h-[480px] overflow-auto rounded-md border">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-muted text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">Time</th>
                  <th className="px-3 py-2 font-medium">Event</th>
                  <th className="px-3 py-2 font-medium">Actor</th>
                  <th className="px-3 py-2 font-medium">IP</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {audit.data?.map((a) => (
                  <tr key={a.id}>
                    <td className="whitespace-nowrap px-3 py-1.5 text-xs text-muted-foreground">{formatDateTime(a.created_at)}</td>
                    <td className="px-3 py-1.5">
                      <code className="text-xs">{a.action}</code>
                      {a.target && <span className="ml-2 text-xs text-muted-foreground">{a.target}</span>}
                    </td>
                    <td className="px-3 py-1.5 text-xs">{a.actor_label || a.actor_type}</td>
                    <td className="px-3 py-1.5 text-xs text-muted-foreground">{a.ip ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
