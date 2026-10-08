import { readFileSync } from "node:fs";
import { expect, type Page, test } from "@playwright/test";
import { freshTotp } from "./totp";

/**
 * Full user journey against a running instance:
 * first login + MFA enrollment → scanner token → upload scanned PDF via the
 * upload API → OCR → inbox → full-text search → view + edit → logout → MFA login.
 *
 * Requires an existing user without MFA (see e2e/README.md).
 */

const USER = process.env.DOCNEST_E2E_USER ?? "e2e";
const PASSWORD = process.env.DOCNEST_E2E_PASSWORD ?? "E2E-Test-Password-123";

test.describe.configure({ mode: "serial" });

let totpSecret = "";
let scannerToken = "";

async function login(page: Page) {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Continue" }).click();
}

test("first login requires enrolling a second factor", async ({ page }) => {
  await login(page);
  await expect(page).toHaveURL(/\/setup/);
  await expect(page.getByText("Protect your account")).toBeVisible();

  await page.getByRole("tab", { name: "Authenticator" }).click();
  await page.getByText("Can't scan? Enter the key manually").click();
  totpSecret = (await page.locator("details code").textContent())?.trim() ?? "";
  expect(totpSecret).toMatch(/^[A-Z2-7]{32}$/);

  await page.getByLabel("Verification code").fill(await freshTotp(totpSecret));
  await page.getByRole("button", { name: "Confirm" }).click();

  await expect(page.getByText("Save your recovery codes")).toBeVisible();
  await expect(page.locator(".font-mono span")).toHaveCount(10);
  await page.getByRole("button", { name: /I have saved them/ }).click();
  await expect(page).toHaveURL(/\/inbox/);
  await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();
});

test("the app is protected: logged-out requests are rejected", async ({ request }) => {
  const r = await request.get("/api/v1/documents");
  expect(r.status()).toBe(401);
});

test("a scanner uploads a scanned letter that gets OCR'd and is searchable", async ({ page, request }) => {
  await login(page);
  await page.getByLabel("6-digit code from your authenticator app").fill(await freshTotp(totpSecret));
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page).toHaveURL(/\/inbox/);

  // create a scanner token
  await page.getByRole("link", { name: "Settings" }).click();
  await page.getByRole("tab", { name: "Scanners" }).click();
  await page.getByRole("button", { name: "Add scanner" }).click();
  await page.getByLabel("Name").fill("E2E scanner");
  await page.getByRole("button", { name: "Create" }).click();
  const tokenInput = page.locator("input[readonly]");
  await expect(tokenInput).toHaveValue(/^dn_scan_/);
  scannerToken = await tokenInput.inputValue();
  await page.getByRole("button", { name: "Done" }).click();

  // the scanner token cannot read the archive
  const denied = await request.get("/api/v1/documents", { headers: { Authorization: `Bearer ${scannerToken}` } });
  expect(denied.status()).toBe(401);

  // upload via the scanner API
  const upload = await request.post("/api/upload/v1/documents", {
    headers: { Authorization: `Bearer ${scannerToken}`, "Idempotency-Key": `e2e-${Date.now()}` },
    multipart: {
      file: { name: "scan.pdf", mimeType: "application/pdf", buffer: readFileSync("fixtures/insurance-scan.pdf") },
      bucket: "private",
      document_type: "auto",
      todo: "true",
    },
  });
  expect(upload.status()).toBe(202);
  const { id } = await upload.json();

  // wait for processing (OCR of a scanned page)
  await expect
    .poll(
      async () => {
        const s = await request.get(`/api/upload/v1/documents/${id}`, {
          headers: { Authorization: `Bearer ${scannerToken}` },
        });
        return (await s.json()).status;
      },
      { timeout: 120_000, intervals: [2000] },
    )
    .toBe("processed");

  // it shows up in the todo list
  await page.getByRole("link", { name: "Todos" }).click();
  await expect(page.getByRole("link", { name: /Fahrzeugversicherung|Allsafe/ }).first()).toBeVisible();

  // full-text search finds a word that only exists in the scanned image
  await page.getByLabel("Search documents").fill("Kraftfahrzeug");
  await page.getByLabel("Search documents").press("Enter");
  await expect(page).toHaveURL(/q=Kraftfahrzeug/);
  await expect(page.getByText("1 document")).toBeVisible();
  await expect(page.locator("mark").first()).toBeVisible();

  // open, view and edit
  await page.locator("main a[href^='/documents/']").nth(1).click();
  await expect(page.locator(".pdf-page canvas").first()).toBeVisible({ timeout: 30_000 });
  const title = page.getByLabel("Title");
  await title.fill("Car insurance 2026");
  await title.press("Enter");
  await page.getByRole("button", { name: "done" }).click();
  await expect(page.getByRole("heading", { name: "Detected" })).toBeVisible();
  await expect(page.getByText("Done", { exact: true }).first()).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Title")).toHaveValue("Car insurance 2026");

  // search by the new title
  await page.getByLabel("Search documents").fill("car insurance");
  await page.getByLabel("Search documents").press("Enter");
  await expect(page.getByText("1 document")).toBeVisible();
});

test("tags were assigned automatically and can be managed", async ({ page }) => {
  await login(page);
  await page.getByLabel("6-digit code from your authenticator app").fill(await freshTotp(totpSecret));
  await page.getByRole("button", { name: "Verify" }).click();
  await page.getByRole("link", { name: "Tags & more" }).click();
  await expect(page.getByText("Suggested by DocNest")).toBeVisible();
  await expect(page.getByText("Insurance", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Senders" }).click();
  await expect(page.getByText(/Allsafe Versicherung AG/)).toBeVisible();
});

test("a passkey can be added and used to sign in", async ({ page }) => {
  // Virtual authenticator (Chrome DevTools Protocol) acting as a platform passkey
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      transport: "internal",
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });

  await login(page);
  await page.getByLabel("6-digit code from your authenticator app").fill(await freshTotp(totpSecret));
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page).toHaveURL(/\/inbox/);

  await page.getByRole("link", { name: "Settings" }).click();
  await page.getByRole("button", { name: "Add security key" }).click();
  await page.getByLabel("Name").fill("Virtual passkey");
  await page.getByRole("button", { name: "Register security key" }).click();
  await expect(page.getByText("Second factor added")).toBeVisible();
  await expect(page.getByText("Virtual passkey")).toBeVisible();

  // sign out and back in with the passkey (offered first automatically)
  await page.getByRole("button", { name: /e2e/ }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page).toHaveURL(/\/inbox/, { timeout: 20_000 });
});

test("recurring payslips are grouped into a series", async ({ page, request }) => {
  for (const month of ["01", "02", "03"]) {
    const r = await request.post("/api/upload/v1/documents", {
      headers: { Authorization: `Bearer ${scannerToken}` },
      multipart: {
        file: { name: `payslip-${month}.pdf`, mimeType: "application/pdf", buffer: readFileSync(`fixtures/payslip-${month}.pdf`) },
        bucket: "private",
      },
    });
    expect(r.status()).toBe(202);
    const { id } = await r.json();
    await expect
      .poll(
        async () =>
          (
            await (
              await request.get(`/api/upload/v1/documents/${id}`, { headers: { Authorization: `Bearer ${scannerToken}` } })
            ).json()
          ).status,
        { timeout: 120_000, intervals: [2000] },
      )
      .toBe("processed");
  }

  await login(page);
  // a passkey is registered now, so it is offered first — switch to the authenticator app
  await page.getByRole("button", { name: "Use authenticator app" }).click();
  await page.getByLabel("6-digit code from your authenticator app").fill(await freshTotp(totpSecret));
  await page.getByRole("button", { name: "Verify" }).click();
  await page.getByRole("link", { name: "Series" }).click();
  await page.getByText(/Muster Software GmbH/).first().click();
  await expect(page.getByText("January 2026")).toBeVisible();
  await expect(page.getByText("February 2026")).toBeVisible();
  await expect(page.getByText("March 2026")).toBeVisible();
  await page.getByRole("button", { name: "Confirm series" }).click();
  await expect(page.getByRole("button", { name: "Confirm series" })).toHaveCount(0);
});

test("PDFs can be uploaded from the web UI by drag & drop and file picker", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "Use authenticator app" }).click();
  await page.getByLabel("6-digit code from your authenticator app").fill(await freshTotp(totpSecret));
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page).toHaveURL(/\/inbox/);

  // 1) drag & drop a PDF onto the page
  const pdf = readFileSync("fixtures/invoice-scan.pdf").toString("base64");
  const dataTransfer = await page.evaluateHandle((b64) => {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], "stromrechnung.pdf", { type: "application/pdf" }));
    return dt;
  }, pdf);
  await page.dispatchEvent("main", "dragenter", { dataTransfer });
  await expect(page.getByText("Drop PDFs to upload")).toBeVisible();
  await page.dispatchEvent("main", "drop", { dataTransfer });
  await expect(page.getByText("stromrechnung.pdf uploaded")).toBeVisible();

  // it is processed like a scan: OCR'd and searchable
  await expect(async () => {
    await page.goto("/documents?q=Stromlieferung");
    await expect(page.getByText("1 document")).toBeVisible({ timeout: 5000 });
  }).toPass({ timeout: 120_000, intervals: [3000] });
  await page.locator("main a[href^='/documents/']").nth(1).click();
  await expect(page.getByText("Web upload")).toBeVisible();

  // 2) the same file again via the upload dialog's file picker -> recognized as duplicate
  await page.getByRole("button", { name: "Upload" }).click();
  await expect(page.getByText("Upload documents")).toBeVisible();
  await page.getByTestId("upload-input").setInputFiles("fixtures/invoice-scan.pdf");
  await expect(page.getByText("invoice-scan.pdf is already in DocNest")).toBeVisible();
});

test("the inbox can be reviewed one document at a time using the OCR text", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "Use authenticator app" }).click();
  await page.getByLabel("6-digit code from your authenticator app").fill(await freshTotp(totpSecret));
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page).toHaveURL(/\/inbox/);

  await page.getByRole("button", { name: /Review inbox/ }).click();
  await expect(page.getByText(/Reviewing 1 of \d+/)).toBeVisible();
  const total = Number((await page.getByText(/Reviewing 1 of \d+/).textContent())?.match(/of (\d+)/)?.[1]);
  await page.getByRole("button", { name: "Text", exact: true }).click();
  const ocr = page.getByTestId("ocr-text");
  await expect(ocr).toBeVisible();

  // select a word in the OCR text and send it to the title field
  const selectText = async (needle: RegExp) => {
    await ocr.evaluate((el, source) => {
      const re = new RegExp(source);
      const node = el.firstChild as Text;
      const m = re.exec(node.data);
      if (!m) throw new Error(`not found: ${source}`);
      const range = document.createRange();
      range.setStart(node, m.index);
      range.setEnd(node, m.index + m[0].length);
      const sel = window.getSelection()!;
      sel.removeAllRanges();
      sel.addRange(range);
      el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    }, needle.source);
  };
  await selectText(/Gehaltsabrechnung \S+ 2026|Rechnung Nr\. \S+|Beitragsrechnung[^\n]*/);
  await page.getByRole("button", { name: "Title", exact: true }).click();
  const title = await page.getByLabel("Title").inputValue();
  expect(title.length).toBeGreaterThan(5);

  await selectText(/\d{2}\.\d{2}\.\d{4}/);
  await page.getByRole("button", { name: "Date", exact: true }).click();
  await expect(page.getByLabel("Document date")).toHaveValue(/^\d{2}\.\d{2}\.\d{4}$/);

  await page.getByRole("button", { name: "Save & next" }).click();
  if (total > 1) await expect(page.getByText(`Reviewing 2 of ${total}`)).toBeVisible();
  else await expect(page.getByText("All done — inbox reviewed")).toBeVisible();

  // the saved document left the inbox with the chosen title
  await page.goto(`/documents?q=${encodeURIComponent(title.split(" ")[0])}&status=done`);
  await expect(page.getByRole("link", { name: title })).toBeVisible();
});

test("sign-ins are logged with device, failures and per-session actions", async ({ page }) => {
  // a failed attempt by "someone else"
  const failedLogin = await page.context().browser()!.newContext();
  const p2 = await failedLogin.newPage();
  await p2.goto("/login");
  await p2.getByLabel("Username").fill(USER);
  await p2.getByLabel("Password").fill("definitely-wrong");
  await p2.getByRole("button", { name: "Continue" }).click();
  await expect(p2.getByText("Invalid username or password")).toBeVisible();
  await failedLogin.close();

  await login(page);
  await page.getByRole("button", { name: "Use authenticator app" }).click();
  await page.getByLabel("6-digit code from your authenticator app").fill(await freshTotp(totpSecret));
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByText(/failed sign-in attempt/)).toBeVisible();
  await expect(page.getByText(/Last sign-in .* from /)).toBeVisible();

  await page.getByRole("link", { name: "Settings" }).click();
  await expect(page.getByText("Sign-in activity")).toBeVisible();
  await expect(page.getByText("This session")).toBeVisible();
  await expect(page.getByText("Wrong password").first()).toBeVisible();
  await expect(page.getByText(/Chrome on \w+ · /).first()).toBeVisible();
  // an earlier session in which documents were opened and edited
  await page.getByRole("button", { name: /authenticator app.*actions/ }).first().click();
  await expect(page.getByText(/Opened|Edited|Uploaded/).first()).toBeVisible();
});

test("documents are filed into folders by drag & drop and the move dialog", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "Use authenticator app" }).click();
  await page.getByLabel("6-digit code from your authenticator app").fill(await freshTotp(totpSecret));
  await page.getByRole("button", { name: "Verify" }).click();
  await page.getByRole("link", { name: "Filing" }).click();
  const tree = page.locator("main aside");

  // scanner uploads with bucket "private" landed in the "Private" folder; web uploads are unfiled
  await expect(page.locator("main").getByRole("link", { name: /^Private/ }).last()).toBeVisible();
  await expect(page.getByText(/Unfiled documents \([1-9]/)).toBeVisible();

  // nested folders can be created in one go
  await page.getByRole("button", { name: "New folder" }).click();
  await page.getByLabel("Name").fill("Haushalt/Strom");
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toContainText("Haushalt");
  await expect(page.getByText("This folder is empty")).toBeVisible();

  // drag an unfiled document onto the folder in the tree
  await tree.getByRole("link", { name: "Unfiled" }).click();
  const row = page.locator("main a[href^='/documents/']").nth(1);
  await row.dragTo(tree.getByRole("link", { name: "Strom" }));
  await expect(page.getByText(/Moved 1 document to Strom/)).toBeVisible();
  await expect(page.getByText("Everything is filed. Nice!")).toBeVisible();

  // and move it on with the dialog
  await tree.getByRole("link", { name: "Strom" }).click();
  await page.getByRole("checkbox", { name: "Select" }).first().check();
  await page.getByRole("button", { name: "Move to…" }).click();
  await page.getByPlaceholder("Search folders…").fill("haushalt");
  await page.getByRole("dialog").getByRole("button", { name: "Haushalt", exact: true }).click();
  await expect(page.getByText(/Moved 1 document to Haushalt/)).toBeVisible();
  await expect(page.getByText("This folder is empty")).toBeVisible();
});

test("scans are enhanced, the original stays available, and paper is put away", async ({ page, request }) => {
  await login(page);
  await page.getByRole("button", { name: "Use authenticator app" }).click();
  await page.getByLabel("6-digit code from your authenticator app").fill(await freshTotp(totpSecret));
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page).toHaveURL(/\/inbox/);

  // a crooked, too long page plus an empty backside, sent as raw scanner images
  const upload = await request.post("/api/upload/v1/documents", {
    headers: { Authorization: `Bearer ${scannerToken}` },
    multipart: {
      file: { name: "p1.png", mimeType: "image/png", buffer: readFileSync("fixtures/crooked-page.png") },
      bucket: "private",
    },
  });
  expect(upload.status()).toBe(202);
  const { id } = await upload.json();
  const blank = await request.post("/api/upload/v1/documents", {
    headers: { Authorization: `Bearer ${scannerToken}` },
    multipart: {
      file: { name: "p2.png", mimeType: "image/png", buffer: readFileSync("fixtures/blank-page.png") },
      bucket: "private",
    },
  });
  expect(blank.status()).toBe(202);
  await expect
    .poll(
      async () =>
        (
          await (
            await request.get(`/api/upload/v1/documents/${id}`, { headers: { Authorization: `Bearer ${scannerToken}` } })
          ).json()
        ).status,
      { timeout: 120_000, intervals: [2000] },
    )
    .toBe("processed");

  // the document shows the enhanced version; the original can be viewed
  await page.goto(`/documents/${id}`);
  await expect(page.locator(".pdf-page canvas").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("Straightened, cropped and cleaned up")).toBeVisible();
  await page.getByRole("radio", { name: "Original" }).click();
  await expect(page.getByText("The file exactly as it was scanned or uploaded")).toBeVisible();
  await expect(page.locator(".pdf-page canvas").first()).toBeVisible({ timeout: 30_000 });
  await page.getByRole("tab", { name: "History" }).click();
  await expect(page.getByText(/straightened/).first()).toBeVisible();
  await expect(page.getByText(/cropped/).first()).toBeVisible();

  // put the paper away in a new binder
  await page.getByRole("link", { name: /^Paper/ }).click();
  await expect(page.getByText(/waiting to be put away/)).toBeVisible();
  await page.getByRole("button", { name: "Put away…" }).click();
  await page.getByRole("button", { name: "New location", exact: true }).click();
  await page.getByLabel("Name").fill("Binder E2E");
  await page.getByRole("button", { name: /^Put away \d+/ }).click();
  await expect(page.getByText("Every paper original has a location.")).toBeVisible();
  await expect(page.getByText("Binder E2E").first()).toBeVisible();

  // the document tells where its paper is
  await page.goto(`/documents/${id}`);
  await expect(page.getByText("Binder E2E").first()).toBeVisible();
  await expect(page.getByText(/from the top/)).toBeVisible();
});

test("screenshots of the main pages", async ({ page }) => {
  test.skip(!process.env.SCREENSHOTS, "set SCREENSHOTS=1 to capture");
  await page.setViewportSize({ width: 1440, height: 900 });
  await login(page);
  await page.getByRole("button", { name: "Use authenticator app" }).click();
  await page.getByLabel("6-digit code from your authenticator app").fill(await freshTotp(totpSecret));
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page).toHaveURL(/\/inbox/);
  const shot = async (name: string) => {
    await page.waitForTimeout(800);
    await page.screenshot({ path: `screenshots/${name}.png`, fullPage: false });
  };
  await shot("inbox");
  await page.goto("/documents");
  await shot("documents");
  await page.goto("/documents?q=Gehaltsabrechnung");
  await shot("search");
  await page.goto("/series");
  await page.getByText(/Muster Software GmbH/).first().click();
  await shot("series");
  await page.goto("/filing");
  await shot("filing");
  await page.goto("/organize");
  await shot("organize");
  await page.goto("/settings");
  await shot("settings");
  await page.getByRole("tab", { name: "System" }).click();
  await page.getByText("Scan enhancement").first().scrollIntoViewIfNeeded();
  await shot("settings-enhancement");
  await page.goto("/paper");
  await shot("paper");
  await page.goto("/documents");
  await page.locator("main a[href^='/documents/']").first().click();
  await expect(page.locator(".pdf-page canvas").first()).toBeVisible({ timeout: 30_000 });
  await shot("document");
  await page.goto("/inbox/review");
  await page.getByRole("button", { name: "Both", exact: true }).click();
  await expect(page.locator(".pdf-page canvas").first()).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("ocr-text").evaluate((el) => {
    const node = el.firstChild as Text;
    const i = node.data.indexOf("Muenchen");
    const range = document.createRange();
    range.setStart(node, Math.max(0, i));
    range.setEnd(node, Math.max(0, i) + 8);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  });
  await shot("review");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/inbox");
  await shot("inbox-dark");
});
