/** Turn a User-Agent string into "Firefox on Linux". */
export function describeAgent(ua?: string | null): string {
  if (!ua) return "Unknown device";
  const browser =
    /Edg\//.test(ua) ? "Edge"
    : /OPR\//.test(ua) ? "Opera"
    : /Firefox\//.test(ua) ? "Firefox"
    : /Chrome\//.test(ua) ? "Chrome"
    : /Safari\//.test(ua) ? "Safari"
    : /curl|python|requests|Go-http/i.test(ua) ? "Script"
    : "Browser";
  const os =
    /iPhone|iPad/.test(ua) ? "iOS"
    : /Android/.test(ua) ? "Android"
    : /Windows/.test(ua) ? "Windows"
    : /Mac OS X|Macintosh/.test(ua) ? "macOS"
    : /CrOS/.test(ua) ? "ChromeOS"
    : /Linux|X11/.test(ua) ? "Linux"
    : "";
  return os ? `${browser} on ${os}` : browser;
}

const METHODS: Record<string, string> = {
  webauthn: "passkey / security key",
  totp: "authenticator app",
  recovery: "recovery code",
  enrollment: "first setup",
  password: "password",
};

export function describeMethod(method?: string | null): string {
  return METHODS[method ?? ""] ?? method ?? "";
}

const ACTIONS: Record<string, string> = {
  "document.opened": "Opened",
  "document.downloaded": "Downloaded",
  "document.updated": "Edited",
  "document.deleted": "Deleted",
  "document.reprocess": "Reprocessed",
  "document.bulk": "Bulk change",
  "upload.accepted": "Uploaded",
  "upload.duplicate": "Uploaded (duplicate)",
  "upload.rejected": "Upload rejected",
  logout: "Signed out",
  "reauth.failed": "Wrong password on confirmation",
  "mfa.totp_added": "Added authenticator app",
  "mfa.webauthn_added": "Added security key",
  "mfa.device_removed": "Removed a second factor",
  "mfa.recovery_codes_generated": "Generated new recovery codes",
  "account.password_changed": "Changed password",
  "account.sessions_revoked": "Signed out other sessions",
  "account.session_revoked": "Signed out a session",
  "scanner.created": "Created scanner",
  "scanner.token_rotated": "Rotated scanner token",
  "scanner.revoked": "Revoked scanner",
  "scanner.deleted": "Deleted scanner",
  "settings.processing_updated": "Changed default processor",
  "tag.merged": "Merged tags",
  "tag.deleted": "Deleted tag",
  "folder.created": "Created folder",
  "folder.moved": "Moved folder",
  "folder.deleted": "Deleted folder",
  "filing.applied": "Filed documents into subfolders",
  "assist.requested": "Asked the assistant for changes",
  "mail.settings": "Changed email inbox",
  "mail.imported": "Imported an email",
};

export function describeAction(action: string): string {
  return ACTIONS[action] ?? action;
}
