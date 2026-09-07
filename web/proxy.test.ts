import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { NextRequest } from "next/server";
import { proxy } from "./proxy";

// Auth configured (H2G_PASSWORD), no session cookie on any request: the shape of a
// Vercel Cron / GitHub Actions caller. The epoch endpoint is stubbed so the proxy
// never fetches.
function req(path: string, headers: Record<string, string> = {}, method = "GET"): NextRequest {
  return new NextRequest(`http://h${path}`, { headers, method });
}
const passedThrough = (res: Response) => res.status === 200 && res.headers.get("x-middleware-next") === "1";

beforeEach(() => {
  process.env.H2G_PASSWORD = "test-pw";
  delete process.env.HEVY2GARMIN_SECRET;
  delete process.env.DEMO_MODE;
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ n: 0 }), { status: 200 })));
});
afterEach(() => {
  delete process.env.H2G_PASSWORD;
  delete process.env.DEMO_MODE;
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

describe("proxy: DEMO_MODE refuses every mutating /api method (#471)", () => {
  const demoBody = { ok: false, error: "Read-only in demo mode" };

  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    it(`${method} /api/mapping is 403 JSON, even with a valid-looking bearer`, async () => {
      process.env.DEMO_MODE = "true";
      const res = await proxy(req("/api/mapping", { authorization: "Bearer x" }, method));
      expect(res.status).toBe(403);
      expect(res.headers.get("content-type")).toContain("application/json");
      expect(await res.json()).toEqual(demoBody);
    });
  }

  it("the refusal comes before auth: a signed-in session is still refused", async () => {
    process.env.DEMO_MODE = "1";
    const res = await proxy(req("/api/unsync-all", {}, "POST"));
    expect(res.status).toBe(403);
    expect(await res.json()).toEqual(demoBody);
  });

  it("a demo with auth disabled is still read-only", async () => {
    process.env.DEMO_MODE = "yes";
    delete process.env.H2G_PASSWORD;
    expect((await proxy(req("/api/settings", {}, "POST"))).status).toBe(403);
    expect(passedThrough(await proxy(req("/api/settings")))).toBe(true);
  });

  it("GET stays readable and login/logout stay allowed in demo", async () => {
    process.env.DEMO_MODE = "on";
    expect(passedThrough(await proxy(req("/api/login", {}, "POST")))).toBe(true);
    expect(passedThrough(await proxy(req("/api/logout", {}, "POST")))).toBe(true);
    // GET /api/settings without a session is the normal auth 401, not the demo 403.
    expect((await proxy(req("/api/settings"))).status).toBe(401);
  });

  it("pages are not affected by demo mode", async () => {
    process.env.DEMO_MODE = "true";
    const res = await proxy(req("/dashboard", {}, "POST"));
    expect(res.headers.get("location")).toBe("http://h/login?next=%2Fdashboard");
  });

  it("with DEMO_MODE off (unset, false, 0) nothing changes", async () => {
    for (const v of [undefined, "false", "0", ""]) {
      if (v === undefined) delete process.env.DEMO_MODE; else process.env.DEMO_MODE = v;
      const res = await proxy(req("/api/mapping", {}, "POST"));
      expect(res.status).toBe(401);
      expect(await res.text()).toBe("Unauthorized");
    }
  });
});
