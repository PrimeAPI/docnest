import { createHmac } from "node:crypto";

function base32Decode(input: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const c of input.replace(/=+$/, "").toUpperCase()) {
    const v = alphabet.indexOf(c);
    if (v < 0) continue;
    bits += v.toString(2).padStart(5, "0");
  }
  const bytes: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(Number.parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}

export function currentStep(): number {
  return Math.floor(Date.now() / 1000 / 30);
}

let lastUsed = 0;

/** A code for a time step that has not been used yet (the server rejects replays). */
export async function freshTotp(secret: string): Promise<string> {
  for (;;) {
    const cur = currentStep();
    for (const off of [0, 1]) {
      if (cur + off > lastUsed) {
        lastUsed = cur + off;
        return codeAt(secret, cur + off);
      }
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

export function totp(secret: string, offsetSteps = 0): string {
  return codeAt(secret, currentStep() + offsetSteps);
}

function codeAt(secret: string, counter: number): string {
  const buf = Buffer.alloc(8);
  buf.writeBigUInt64BE(BigInt(counter));
  const hmac = createHmac("sha1", base32Decode(secret)).update(buf).digest();
  const offset = hmac[hmac.length - 1] & 0xf;
  const code = (hmac.readUInt32BE(offset) & 0x7fffffff) % 1_000_000;
  return code.toString().padStart(6, "0");
}
