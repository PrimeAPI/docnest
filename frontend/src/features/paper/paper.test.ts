import { describe, expect, it } from "vitest";
import { formatThickness } from "./paper";

describe("formatThickness", () => {
  it("shows millimetres for thin stacks and centimetres for thick ones", () => {
    expect(formatThickness(3)).toBe("0,3 mm");
    expect(formatThickness(42)).toBe("4,2 mm");
    expect(formatThickness(250)).toBe("2,5 cm");
  });
});
