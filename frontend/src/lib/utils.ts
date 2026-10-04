import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const dateFmt = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" });
const dateTimeFmt = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatDate(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value.length === 10 ? `${value}T00:00:00` : value);
  return Number.isNaN(d.getTime()) ? value : dateFmt.format(d);
}

export function formatDateTime(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : dateTimeFmt.format(d);
}

export function relativeTime(value?: string | null): string {
  if (!value) return "—";
  const diff = (Date.now() - new Date(value).getTime()) / 1000;
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (diff < 60) return "just now";
  if (diff < 3600) return rtf.format(-Math.round(diff / 60), "minute");
  if (diff < 86400) return rtf.format(-Math.round(diff / 3600), "hour");
  if (diff < 86400 * 30) return rtf.format(-Math.round(diff / 86400), "day");
  return formatDate(value);
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export const TAG_COLORS = [
  "slate", "red", "orange", "amber", "lime", "emerald", "teal", "sky", "indigo", "violet", "pink",
] as const;

const colorClasses: Record<string, string> = {
  slate: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200",
  red: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-200",
  orange: "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-200",
  amber: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  lime: "bg-lime-100 text-lime-800 dark:bg-lime-950 dark:text-lime-200",
  emerald: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-200",
  teal: "bg-teal-100 text-teal-700 dark:bg-teal-950 dark:text-teal-200",
  sky: "bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-200",
  indigo: "bg-indigo-100 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-200",
  violet: "bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-200",
  pink: "bg-pink-100 text-pink-700 dark:bg-pink-950 dark:text-pink-200",
};

export function colorClass(color?: string | null): string {
  return colorClasses[color ?? "slate"] ?? colorClasses.slate;
}

const dotClasses: Record<string, string> = {
  slate: "bg-slate-400", red: "bg-red-500", orange: "bg-orange-500", amber: "bg-amber-500", lime: "bg-lime-500",
  emerald: "bg-emerald-500", teal: "bg-teal-500", sky: "bg-sky-500", indigo: "bg-indigo-500",
  violet: "bg-violet-500", pink: "bg-pink-500",
};

export function dotClass(color?: string | null): string {
  return dotClasses[color ?? "slate"] ?? dotClasses.slate;
}
