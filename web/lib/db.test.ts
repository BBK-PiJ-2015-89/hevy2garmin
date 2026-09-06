import { describe, it, expect, vi, beforeEach } from "vitest";

// A fake postgres.js client: callable as a tagged template, with `unsafe` and `json`.
const calls: string[] = [];
vi.mock("postgres", () => ({
  default: () => {
    const client = (strings: TemplateStringsArray) => { calls.push(`query:${strings.join("?").trim()}`); return Promise.resolve([{ ok: 1 }]); };
    client.unsafe = (text: string) => { calls.push(`unsafe:${text.split("\n")[0].trim()}`); return Promise.resolve([]); };
    client.json = (v: unknown) => v;
    return client;
  },
}));

import { getDb } from "./db";
import { SCHEMA_STATEMENTS, resetSchemaForTests } from "./schema";

beforeEach(() => { calls.length = 0; resetSchemaForTests(); process.env.DATABASE_URL = "postgresql://x"; });

describe("getDb runs the schema bootstrap before the first query (#475)", () => {
  it("the CREATEs precede the query, and later queries do not repeat them", async () => {
    const sql = getDb();
    const rows = await sql`SELECT 1`;
    expect(rows).toEqual([{ ok: 1 }]);
    const unsafeCount = calls.filter((c) => c.startsWith("unsafe:")).length;
    expect(unsafeCount).toBe(SCHEMA_STATEMENTS.length);
    expect(calls.indexOf("query:SELECT 1")).toBe(SCHEMA_STATEMENTS.length);
    await sql`SELECT 2`;
    expect(calls.filter((c) => c.startsWith("unsafe:")).length).toBe(SCHEMA_STATEMENTS.length);
  });
});
