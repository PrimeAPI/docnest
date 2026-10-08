import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Archive,
  CheckSquare,
  FileText,
  FolderTree,
  Inbox,
  Layers,
  LogOut,
  Menu,
  Monitor,
  Moon,
  Search,
  Settings,
  Sun,
  Tags,
  Upload,
  X,
} from "lucide-react";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate, useSearchParams } from "react-router";
import { call, client } from "@/api/client";
import { keys, useOverview, useSession, useSystem } from "@/api/queries";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown";
import { Input } from "@/components/ui/input";
import { Kbd } from "@/components/ui/misc";
import { UploadProvider, useUpload } from "@/features/upload/upload-provider";
import { ProcessingQueueButton } from "@/features/processing/processing-queue";
import { getTheme, setTheme, type Theme } from "@/lib/theme";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/inbox", label: "Inbox", icon: Inbox, count: "inbox" },
  { to: "/documents", label: "Documents", icon: FileText },
  { to: "/filing", label: "Filing", icon: FolderTree },
  { to: "/todos", label: "Todos", icon: CheckSquare, count: "todo" },
  { to: "/series", label: "Series", icon: Layers },
  { to: "/paper", label: "Paper", icon: Archive, count: "paper" },
  { to: "/organize", label: "Tags & more", icon: Tags, count: "suggested" },
  { to: "/settings", label: "Settings", icon: Settings },
] as const;

export function AppLayout() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  useEffect(() => setMobileOpen(false), [location.pathname]);

  return (
    <UploadProvider>
    <div className="flex h-full">
      <aside className="hidden w-60 shrink-0 border-r bg-sidebar md:flex md:flex-col">
        <Sidebar />
      </aside>
      {mobileOpen && (
        <div className="fixed inset-0 z-40 md:hidden">
          <div className="absolute inset-0 bg-black/40" onClick={() => setMobileOpen(false)} />
          <aside className="absolute inset-y-0 left-0 flex w-64 flex-col border-r bg-sidebar shadow-xl">
            <Sidebar />
          </aside>
        </div>
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar onMenu={() => setMobileOpen((v) => !v)} menuOpen={mobileOpen} />
        <StatusBanner />
        <main className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-7xl px-4 py-6 md:px-8">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
    </UploadProvider>
  );
}

function Sidebar() {
  const overview = useOverview();
  const o = overview.data;
  const counts: Record<string, number | undefined> = {
    inbox: o ? o.new + o.failed : undefined,
    todo: o?.todo,
    suggested: o?.suggested_tags,
    paper: o?.paper_pending,
  };
  return (
    <>
      <div className="flex h-14 items-center gap-2 px-5">
        <img src="/favicon.svg" alt="" className="size-7" />
        <span className="text-lg font-semibold tracking-tight">DocNest</span>
      </div>
      <nav className="flex flex-1 flex-col gap-0.5 px-3 py-2">
        {NAV.map((item) => {
          const count = "count" in item ? counts[item.count] : undefined;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                  isActive && "bg-accent text-accent-foreground hover:bg-accent hover:text-accent-foreground",
                )
              }
            >
              <item.icon className="size-4" />
              <span className="flex-1">{item.label}</span>
              {!!count && (
                <span className="rounded-full bg-primary px-1.5 py-0.5 text-[10px] font-semibold leading-none text-primary-foreground">
                  {count}
                </span>
              )}
            </NavLink>
          );
        })}
      </nav>
      {o && (
        <div className="px-5 pb-2 text-xs text-muted-foreground">{o.total} documents</div>
      )}
      <ProcessingQueueButton />
    </>
  );
}

function TopBar({ onMenu, menuOpen }: { onMenu: () => void; menuOpen: boolean }) {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const location = useLocation();
  const [q, setQ] = useState(location.pathname === "/documents" ? (params.get("q") ?? "") : "");
  const input = useRef<HTMLInputElement>(null);
  const session = useSession();
  const qc = useQueryClient();
  const [theme, setThemeState] = useState<Theme>(getTheme());
  const upload = useUpload();

  useEffect(() => {
    if (location.pathname === "/documents") setQ(params.get("q") ?? "");
  }, [location.pathname, params]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) && !target.isContentEditable) {
        e.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const next = new URLSearchParams(location.pathname === "/documents" ? params : undefined);
    if (q.trim()) next.set("q", q.trim());
    else next.delete("q");
    next.delete("page");
    navigate(`/documents?${next.toString()}`);
  };

  const logout = async () => {
    await call(() => client.POST("/api/v1/auth/logout"));
    qc.clear();
    navigate("/login", { replace: true });
  };

  const chooseTheme = (t: Theme) => {
    setTheme(t);
    setThemeState(t);
  };

  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b bg-background px-4 md:px-8">
      <Button variant="ghost" size="icon" className="md:hidden" onClick={onMenu} aria-label="Menu">
        {menuOpen ? <X /> : <Menu />}
      </Button>
      <form onSubmit={submit} className="relative max-w-xl flex-1">
        <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          ref={input}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search all documents…"
          className="pl-9 pr-10"
          aria-label="Search documents"
        />
        <span className="absolute right-3 top-1/2 hidden -translate-y-1/2 sm:block">
          <Kbd>/</Kbd>
        </span>
      </form>
      <div className="ml-auto flex items-center gap-1">
        <Button variant="outline" size="sm" onClick={upload.openDialog} title="Upload PDFs (or drop them anywhere)">
          <Upload /> <span className="hidden sm:inline">Upload</span>
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="sm" className="gap-2">
              <span className="flex size-7 items-center justify-center rounded-full bg-accent text-xs font-semibold uppercase text-accent-foreground">
                {session.data?.username?.slice(0, 2)}
              </span>
              <span className="hidden sm:inline">{session.data?.username}</span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent>
            <DropdownMenuItem onSelect={() => chooseTheme("light")}>
              <Sun /> Light {theme === "light" && "✓"}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => chooseTheme("dark")}>
              <Moon /> Dark {theme === "dark" && "✓"}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => chooseTheme("system")}>
              <Monitor /> System {theme === "system" && "✓"}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => navigate("/settings")}>
              <Settings /> Settings
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={logout}>
              <LogOut /> Sign out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}

function StatusBanner() {
  const system = useSystem();
  const s = system.data;
  if (!s) return null;
  const problems: string[] = [];
  if (s.storage.needs_reauth) {
    problems.push(
      "Proton Drive needs a new login. New documents are kept safely and stored once you run: docker compose exec app docnest proton-login",
    );
  } else if (s.storage.ok === false) {
    problems.push(`Storage is not reachable: ${s.storage.message}`);
  }
  if (s.backup.last_error && !s.storage.needs_reauth) {
    problems.push(`The last database backup failed: ${s.backup.last_error}`);
  }
  if (!s.worker_online) problems.push("The processing worker is not running — new documents will wait.");
  if (!problems.length) return null;
  return (
    <div className="border-b border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900 md:px-8 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-100">
      {problems.map((p) => (
        <div key={p} className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <span>{p}</span>
        </div>
      ))}
    </div>
  );
}

export function useRefreshSession() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: keys.session });
}
