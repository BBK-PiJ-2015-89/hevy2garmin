import { describe, it, expect, vi } from "vitest";
const queries: string[] = [];
vi.mock("./db", () => { const tag = (async (strings: TemplateStringsArray) => { queries.push(strings.join("?").replace(/\s+/g, " ").trim()); return []; }) as unknown as { (): unknown; json: <T>(v: T) => T }; tag.json = (v) => v; return { getDb: () => tag }; });
import { normalizeGarminTokenRow, GARMIN_TOKEN_PLATFORM } from "./garmin-upload";
import { getDb } from "./db";

describe("normalizeGarminTokenRow (#459)", () => {
  it("nests a flat DI payload under garmin_tokens, idempotently, and only once per process", async () => {
    await normalizeGarminTokenRow(getDb()); await normalizeGarminTokenRow(getDb());
    expect(queries.length).toBe(1);
    expect(queries[0]).toContain("UPDATE platform_credentials SET credentials = jsonb_build_object('garmin_tokens', credentials)");
    expect(queries[0]).toContain("credentials ? 'di_token'"); expect(queries[0]).toContain("NOT (credentials ? 'garmin_tokens')");
    expect(GARMIN_TOKEN_PLATFORM).toBe("garmin_tokens");
  });
});
