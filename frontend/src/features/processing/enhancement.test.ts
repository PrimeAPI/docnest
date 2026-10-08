import { describe, expect, it } from "vitest";
import { describeEnhancement } from "./enhancement";

describe("describeEnhancement", () => {
  it("lists what was changed", () => {
    expect(
      describeEnhancement({ pages: 3, scanned_pages: 3, rotated: 1, deskewed: 2, cropped: 0, cleaned: 0, removed_blank: 1 }),
    ).toBe("1 rotated · 2 straightened · 1 blank page removed");
  });
  it("explains when nothing was done", () => {
    expect(describeEnhancement({ pages: 1, scanned_pages: 1 })).toBe("No changes were needed");
    expect(describeEnhancement({ pages: 1, scanned_pages: 0 })).toBe("No scanned pages (nothing to enhance)");
    expect(describeEnhancement(undefined)).toBe("");
  });
});
