import { NextResponse } from "next/server";
import { syncOneWorkout, type SyncOneResult } from "@/lib/sync-one";
import { getDb } from "@/lib/db";
import { getGithubPat, getGithubRepo, triggerViaActions } from "@/lib/github";

// Runs the sync at request time — never at build.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/**
 * GET /api/cron/sync  —  the scheduled-trigger entry (Vercel cron / any
 * scheduler). Requires `Authorization: Bearer <CRON_SECRET>`. Then, like the
 * live path of POST /api/sync, it hands off to the GitHub Action when
 * GITHUB_PAT + GITHUB_REPO are set (the deployed path, off the request), or
 * loops the tested single-workout engine up to a cap. Mirrors the Python
 * /api/cron/sync.
 */

const CAP = 50;


export async function GET(request: Request) {
  const secret = process.env.CRON_SECRET;
  const auth = request.headers.get("authorization") ?? "";
  const m = auth.match(/^Bearer\s+(.+)$/i);
  if (!secret || !m || m[1] !== secret) {
    return NextResponse.json({ ok: false, error: "Unauthorized." }, { status: 401 });
  }

  // Deployed path: hand off to the Action so the long browser-auth sync runs off
  // the request.
  // Settings row first, GITHUB_PAT fallback (#458). The DB handle may be unavailable here; env still works.
  let sqlForPat: ReturnType<typeof getDb> | null = null;
  try { sqlForPat = getDb(); } catch { sqlForPat = null; }
  const pat = await getGithubPat(sqlForPat);
  const repo = getGithubRepo();
  if (pat && repo) {
    try {
      const ok = await triggerViaActions(pat, repo);
      return NextResponse.json({ ok, mode: "dispatch", triggered: ok }, { status: ok ? 200 : 502 });
    } catch (err) {
      const error = err instanceof Error ? err.message : String(err);
      return NextResponse.json({ ok: false, error }, { status: 502 });
    }
  }

  // Local/self-hosted path: loop the tested engine.
  let sql: ReturnType<typeof getDb>;
  try {
    sql = getDb();
  } catch (err) {
    const error = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ ok: false, error: `DB unavailable: ${error}` }, { status: 503 });
  }

  const runs: SyncOneResult[] = [];
  try {
    for (let i = 0; i < CAP; i++) {
      const r = await syncOneWorkout(sql, { dryRun: false });
      if (r.status === "none") break;
      runs.push(r);
      if (r.status === "error") break;
    }
  } catch (err) {
    const error = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ ok: false, error, ran: runs.length }, { status: 500 });
  }

  const synced = runs.filter((r) => r.status === "synced").length;
  return NextResponse.json({ ok: true, mode: "inline", ran: runs.length, synced });
}
