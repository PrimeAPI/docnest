import { describe, expect, it } from "vitest";
import { parseDate } from "./review-page";

describe("parseDate (OCR selection → date field)", () => {
  it.each([
    ["14.03.2026", "2026-03-14"],
    ["Musterstadt, 1.2.26", "2026-02-01"],
    ["2026-03-14", "2026-03-14"],
    ["3. März 2026", "2026-03-03"],
    ["12 December 2025", "2025-12-12"],
    ["March 5, 2026", "2026-03-05"],
  ])("%s → %s", (input, expected) => expect(parseDate(input)).toBe(expected));

  it("rejects non-dates and impossible dates", () => {
    expect(parseDate("Rechnung Nr. 42")).toBeNull();
    expect(parseDate("31.02.2026")).toBeNull();
  });
});
