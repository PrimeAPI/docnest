import { startAuthentication, startRegistration, WebAuthnAbortService } from "@simplewebauthn/browser";

export function webauthnSupported(): boolean {
  return typeof window !== "undefined" && "PublicKeyCredential" in window;
}

// biome-ignore lint: options come from the server verbatim
export async function createCredential(options: any) {
  return startRegistration({ optionsJSON: options });
}

// biome-ignore lint: options come from the server verbatim
export async function getAssertion(options: any) {
  return startAuthentication({ optionsJSON: options });
}

export function webauthnErrorMessage(e: unknown): string {
  if (e instanceof Error) {
    if (e.name === "NotAllowedError") return "The request was cancelled or timed out.";
    if (e.name === "InvalidStateError") return "This security key is already registered.";
    return e.message;
  }
  return "Security key operation failed.";
}

/** Cancel a pending passkey prompt (e.g. when the user switches to another method). */
export function cancelWebauthn() {
  WebAuthnAbortService.cancelCeremony();
}
