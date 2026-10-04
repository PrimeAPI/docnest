import { describe, expect, it } from "vitest";
import { colorClass, formatBytes, formatDate } from "./utils";

describe("utils", () => {
  it("formats sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });
  it("formats empty dates as a dash", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDate("2026-03-14")).toMatch(/2026/);
  });
  it("falls back to slate for unknown colors", () => {
    expect(colorClass("nope")).toBe(colorClass("slate"));
  });
});
