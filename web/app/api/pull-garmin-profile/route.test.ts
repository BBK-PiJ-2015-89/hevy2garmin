import { describe, it, expect, vi } from "vitest";
vi.mock("@/lib/auth", () => ({ authEnabled: () => false, verifySession: async () => true, SESSION_COOKIE: "h2g_session" }));
const writes: { key: string; value: Record<string, unknown> }[] = [];
vi.mock("@/lib/db", () => { const tag = (async (strings: TemplateStringsArray, ...values: unknown[]) => { const q = strings.join("?"); if (q.startsWith("SELECT value FROM app_cache")) return [{ value: { timezone: "Europe/Athens", weight_kg: 70 } }]; if (q.includes("INSERT INTO app_cache")) { writes.push({ key: values[0] as string, value: values[1] as Record<string, unknown> }); return []; } throw new Error("unexpected: " + q); }) as unknown as { (): unknown; json: <T>(v: T) => T }; tag.json = (v) => v; return { getDb: () => tag }; });
const { connectapi } = vi.hoisted(() => ({ connectapi: vi.fn(async () => ({ userData: { weight: 73250, birthDate: "1994-07-01", gender: "MALE", vo2MaxRunning: 52.4 } })) }));
vi.mock("@/lib/garmin-upload", () => ({ getGarminClient: async () => ({ connectapi }) }));
import { POST } from "./route";

describe("POST /api/pull-garmin-profile", () => {
  it("maps Garmin user-settings into user_profile with the Python units and keeps other keys", async () => {
    const res = await POST(); const j = await res.json();
    expect(res.status).toBe(200); expect(j.ok).toBe(true);
    expect(j.profile).toEqual({ weight_kg: 73.3, birth_year: 1994, sex: "male", vo2max: 52.4 });
    expect(writes[0].key).toBe("user_profile"); expect(writes[0].value).toEqual({ timezone: "Europe/Athens", weight_kg: 73.3, birth_year: 1994, sex: "male", vo2max: 52.4 });
    expect(connectapi).toHaveBeenCalledWith("/userprofile-service/userprofile/user-settings");
  });
});
