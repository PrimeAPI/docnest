import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Mail, PlugZap, RefreshCw, TriangleAlert } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { formatDateTime } from "@/lib/utils";

type Settings = Schemas["MailSettingsOut"];
type Form = Omit<Schemas["MailSettingsIn"], "allowed_senders" | "password"> & { senders: string; password: string };

const KEY = ["mail-settings"] as const;
const DEFAULT_PORT = { ssl: 993, starttls: 143 } as const;

function toForm(s: Settings): Form {
  return {
    enabled: s.enabled,
    host: s.host,
    port: s.port,
    security: s.security,
    verify_tls: s.verify_tls,
    username: s.username,
    folder: s.folder,
    interval_minutes: s.interval_minutes,
    senders: s.allowed_senders.join("\n"),
    password: "",
  };
}

function toBody(f: Form): Schemas["MailSettingsIn"] {
  const { senders, password, ...rest } = f;
  return {
    ...rest,
    password: password ? password : null, // empty: keep the saved password
    allowed_senders: senders
      .split(/[\n,;]+/)
      .map((s) => s.trim())
      .filter(Boolean),
  };
}

export function MailInboxCard() {
  const qc = useQueryClient();
  const settings = useQuery({
    queryKey: KEY,
    queryFn: () => call(() => client.GET("/api/v1/mail/settings")),
    refetchInterval: 15_000,
  });
  const [form, setForm] = useState<Form | null>(null);
  useEffect(() => {
    if (settings.data && form === null) setForm(toForm(settings.data));
  }, [settings.data, form]);

  const save = useMutation({
    mutationFn: (f: Form) => call(() => client.PUT("/api/v1/mail/settings", { body: toBody(f) })),
    onSuccess: (data) => {
      qc.setQueryData(KEY, data);
      setForm(toForm(data));
      toast.success(data.enabled ? "Email inbox saved and turned on" : "Email inbox saved");
    },
    onError: (e) => toast.error(e.message),
  });
  const test = useMutation({
    mutationFn: (f: Form) => call(() => client.POST("/api/v1/mail/test", { body: toBody(f) })),
    onSuccess: (r) =>
      r.ok
        ? toast.success(`Connected. ${r.waiting} message${r.waiting === 1 ? "" : "s"} waiting in the folder.`)
        : toast.error(r.message),
    onError: (e) => toast.error(e.message),
  });
  const check = useMutation({
    mutationFn: () => call(() => client.POST("/api/v1/mail/check")),
    onSuccess: () => {
      toast.success("Checking the inbox now");
      setTimeout(() => qc.invalidateQueries({ queryKey: KEY }), 5000);
    },
    onError: (e) => toast.error(e.message),
  });

  if (settings.isPending || !form) return settings.error ? <ErrorNote error={settings.error} /> : <Spinner />;
  const saved = settings.data;
  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm({ ...form, [key]: value });
  const senderCount = toBody(form).allowed_senders?.length ?? 0;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate(form);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Mail className="size-5" /> Email inbox
          {saved?.enabled ? <Badge variant="success">on</Badge> : <Badge variant="muted">off</Badge>}
        </CardTitle>
        <CardDescription>
          Forward an email to a mailbox set up only for DocNest. Its attached PDFs and pictures become documents, and the
          email itself is kept as a PDF. The text of the email helps the AI model understand the attachments. The
          documents are processed as usual but never filed into a folder. Imported emails are deleted from the mailbox.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {saved && <Status settings={saved} />}
        <form onSubmit={submit} className="mt-4 grid gap-4 sm:grid-cols-2">
          <label className="flex cursor-pointer items-center gap-2 text-sm font-medium sm:col-span-2">
            <Checkbox checked={form.enabled} onCheckedChange={(v) => set("enabled", v === true)} />
            Import from this inbox
          </label>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="mail-host">IMAP server</Label>
            <Input id="mail-host" value={form.host} placeholder="imap.example.org" onChange={(e) => set("host", e.target.value)} />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="mail-security">Encryption</Label>
              <Select
                id="mail-security"
                value={form.security}
                onChange={(e) => {
                  const security = e.target.value as Form["security"];
                  const port = form.port === DEFAULT_PORT[form.security] ? DEFAULT_PORT[security] : form.port;
                  setForm({ ...form, security, port });
                }}
              >
                <option value="ssl">SSL/TLS</option>
                <option value="starttls">STARTTLS</option>
              </Select>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="mail-port">Port</Label>
              <Input
                id="mail-port"
                type="number"
                min={1}
                max={65535}
                value={form.port}
                onChange={(e) => set("port", Number(e.target.value))}
              />
            </div>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="mail-user">User name</Label>
            <Input id="mail-user" autoComplete="off" value={form.username} onChange={(e) => set("username", e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="mail-password">Password</Label>
            <Input
              id="mail-password"
              type="password"
              autoComplete="new-password"
              value={form.password}
              placeholder={saved?.has_password ? "Saved — leave empty to keep it" : ""}
              onChange={(e) => set("password", e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="mail-folder">Folder</Label>
            <Input id="mail-folder" value={form.folder} onChange={(e) => set("folder", e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="mail-interval">Check every … minutes</Label>
            <Input
              id="mail-interval"
              type="number"
              min={1}
              max={1440}
              value={form.interval_minutes}
              onChange={(e) => set("interval_minutes", Number(e.target.value))}
            />
          </div>
          <label className="flex cursor-pointer items-start gap-2 text-sm sm:col-span-2">
            <Checkbox className="mt-0.5" checked={form.verify_tls} onCheckedChange={(v) => set("verify_tls", v === true)} />
            <span>
              Check the server's certificate
              <span className="block text-xs text-muted-foreground">
                Turn this off only for a bridge on your own network with a self-signed certificate, such as Proton Mail
                Bridge.
              </span>
            </span>
          </label>
          <div className="flex flex-col gap-1.5 sm:col-span-2">
            <Label htmlFor="mail-senders">Allowed senders</Label>
            <Textarea
              id="mail-senders"
              rows={3}
              value={form.senders}
              placeholder={"me@example.org\n@my-company.example"}
              onChange={(e) => set("senders", e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              One address per line, or <code>@domain</code> for a whole domain: the addresses you forward from. Only
              their mail is imported. Mail from anyone else is left in the inbox untouched, and nothing is imported while
              this list is empty.
            </p>
            {form.enabled && senderCount === 0 && (
              <p className="flex items-center gap-1 text-xs text-amber-700 dark:text-amber-300">
                <TriangleAlert className="size-3.5" /> Add at least one sender to turn the inbox on.
              </p>
            )}
          </div>
          <div className="flex flex-wrap gap-2 sm:col-span-2">
            <Button type="submit" disabled={save.isPending}>
              Save
            </Button>
            <Button type="button" variant="outline" disabled={test.isPending} onClick={() => test.mutate(form)}>
              <PlugZap /> {test.isPending ? "Testing…" : "Test connection"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={!saved?.enabled || check.isPending}
              onClick={() => check.mutate()}
            >
              <RefreshCw /> Check now
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function Status({ settings }: { settings: Settings }) {
  const s = settings.status;
  if (!s.checked_at) {
    return <p className="text-sm text-muted-foreground">Not checked yet.</p>;
  }
  return (
    <div className="flex flex-col gap-1 rounded-md border px-3 py-2 text-sm">
      <span className="flex items-center gap-2">
        {s.ok ? (
          <CheckCircle2 className="size-4 text-emerald-600" />
        ) : (
          <TriangleAlert className="size-4 text-amber-600" />
        )}
        Last checked {formatDateTime(s.checked_at)}
        {s.ok ? "" : " — with problems"}
      </span>
      {s.message && <span className="text-muted-foreground">{s.message}</span>}
      <span className="text-muted-foreground">
        {s.imported_total} email{s.imported_total === 1 ? "" : "s"} imported so far
        {s.last_import_at ? `, the last on ${formatDateTime(s.last_import_at)} (${s.last_documents} new documents)` : ""}.
        {s.ignored ? ` ${s.ignored} email${s.ignored === 1 ? " is" : "s are"} from senders not on the list and stay${s.ignored === 1 ? "s" : ""} in the inbox.` : ""}
      </span>
    </div>
  );
}
