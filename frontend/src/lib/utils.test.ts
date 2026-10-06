import { describe, expect, it } from "vitest";
import { colorClass, formatBytes, formatDate, formatDateInput, formatDateTime, parseDateInput } from "./utils";

describe("utils", () => {
  it("formats sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });
  it("formats dates consistently as DD/MM/YYYY", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDate("2026-03-14")).toBe("14/03/2026");
    expect(formatDateInput("2026-03-04")).toBe("04/03/2026");
    expect(formatDateTime("2026-03-14T15:06:00")).toBe("14/03/2026, 15:06");
  });
  it("parses DD/MM/YYYY inputs and rejects impossible dates", () => {
    expect(parseDateInput("14/03/2026")).toBe("2026-03-14");
    expect(parseDateInput("1.2.2026")).toBe("2026-02-01");
    expect(parseDateInput("31/02/2026")).toBeNull();
  });
  it("falls back to slate for unknown colors", () => {
    expect(colorClass("nope")).toBe(colorClass("slate"));
  });
});
