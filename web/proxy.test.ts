import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { NextRequest } from "next/server";
import { proxy } from "./proxy";

// Auth configured (H2G_PASSWORD), no session cookie on any request: the shape of a
// Vercel Cron / GitHub Actions caller. The epoch endpoint is stubbed so the proxy
// never fetches.
function req(path: string, headers: Record<string, string> = {}): NextRequest {
  return new NextRequest(`http://h${path}`, { headers });
}
const passedThrough = (res: Response) => res.status === 200 && res.headers.get("x-middleware-next") === "1";

beforeEach(() => {
  process.env.H2G_PASSWORD = "test-pw";
  delete process.env.HEVY2GARMIN_SECRET;
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ n: 0 }), { status: 200 })));
});
afterEach(() => {
  delete process.env.H2G_PASSWORD;
  vi.unstubAllGlobals();
});

describe("proxy: /api/cron is public so the route's own CRON_SECRET check runs (#473)", () => {
  it("a bearer on /api/cron/sync reaches the route", async () => {
    const res = await proxy(req("/api/cron/sync", { authorization: "Bearer test-cron-secret" }));
    expect(passedThrough(res)).toBe(true);
  });

  it("/api/cron/webhook passes too; the route, not the proxy, decides on the secret", async () => {
    const res = await proxy(req("/api/cron/webhook"));
    expect(passedThrough(res)).toBe(true);
  });

  it("/api/cronjobs is NOT public: the prefix match is on the path segment", async () => {
    const res = await proxy(req("/api/cronjobs/x", { authorization: "Bearer test-cron-secret" }));
    expect(res.status).toBe(401);
  });

  it("an ordinary API route without a session stays gated with the proxy's plain 401", async () => {
    const res = await proxy(req("/api/settings", { authorization: "Bearer test-cron-secret" }));
    expect(res.status).toBe(401);
    expect(await res.text()).toBe("Unauthorized");
  });

  it("a page without a session is redirected to /login with ?next=", async () => {
    const res = await proxy(req("/dashboard"));
    expect(res.status).toBeGreaterThanOrEqual(300);
    expect(res.headers.get("location")).toBe("http://h/login?next=%2Fdashboard");
  });

  it("the login and epoch endpoints stay public", async () => {
    expect(passedThrough(await proxy(req("/login")))).toBe(true);
    expect(passedThrough(await proxy(req("/api/login")))).toBe(true);
    expect(passedThrough(await proxy(req("/api/session-epoch")))).toBe(true);
  });

  it("with auth disabled everything is open, including /api/settings", async () => {
    delete process.env.H2G_PASSWORD;
    expect(passedThrough(await proxy(req("/api/settings")))).toBe(true);
  });
});
