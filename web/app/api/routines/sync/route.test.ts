import { describe, it, expect, vi } from "vitest";
vi.mock("@/lib/auth", () => ({ authEnabled: () => false, verifySession: async () => true, SESSION_COOKIE: "h2g_session" }));
vi.mock("@/lib/db", () => { const tag = (async () => []) as unknown as { (): unknown; json: <T>(v: T) => T }; tag.json = (v) => v; return { getDb: () => tag }; });
vi.mock("@/lib/hevy-routines", () => ({ fetchHevyRoutines: async () => [{ id: "a", title: "Push" }, { id: "b", title: "Pull" }, { id: "c", title: "Legs" }] }));
vi.mock("@/lib/garmin-routine-sync", () => ({ syncRoutine: async (r: { id: string }) => (r.id === "b" ? { status: "error", error: "boom" } : { status: "synced", garminWorkoutId: `g-${r.id}` }) }));
import { POST } from "./route";
const req = (body?: unknown) => new Request("http://h/api/routines/sync", { method: "POST", headers: { "content-type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });

describe("POST /api/routines/sync (bulk)", () => {
  it("syncs every routine and aggregates like the Python stats", async () => {
    const res = await POST(req()); const j = await res.json();
    expect(res.status).toBe(200); expect(j).toMatchObject({ ok: false, total: 3, synced: 2, failed: 1 });
    expect(j.results.map((r: { id: string; status: string }) => `${r.id}:${r.status}`)).toEqual(["a:synced", "b:error", "c:synced"]);
  });
  it("limits to the given ids", async () => {
    const j = await (await POST(req({ ids: ["c"] }))).json(); expect(j).toMatchObject({ ok: true, total: 1, synced: 1, failed: 0 });
  });
});
