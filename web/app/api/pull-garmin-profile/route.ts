import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { getDb } from "@/lib/db";
import { verifySession, SESSION_COOKIE, authEnabled } from "@/lib/auth";
import { getGarminClient } from "@/lib/garmin-upload";
import { saveConfigKey } from "@/lib/config";

export const dynamic = "force-dynamic";

/**
 * POST /api/pull-garmin-profile — import weight, birth year, sex and VO2max from Garmin into
 * app_cache 'user_profile' (port of the Python route; same fields, same units: kg, year,
 * 'male'|'female', ml/kg/min). Returns JSON instead of an HTML toast (#461).
 */
export async function POST() {
  if (authEnabled()) {
    const store = await cookies();
    if (!(await verifySession(store.get(SESSION_COOKIE)?.value ?? null))) return NextResponse.json({ ok: false, error: "Unauthorized" }, { status: 401 });
  }
  let sql: ReturnType<typeof getDb>;
  try { sql = getDb(); } catch (err) { return NextResponse.json({ ok: false, error: `DB unavailable: ${err instanceof Error ? err.message : String(err)}` }, { status: 503 }); }
  try {
    const client = await getGarminClient();
    const raw = (await client.connectapi("/userprofile-service/userprofile/user-settings")) as { userData?: Record<string, unknown> };
    const u = raw?.userData ?? {};
    const patch: Record<string, unknown> = {};
    const w = Number(u.weight); if (Number.isFinite(w) && w > 0) patch.weight_kg = Math.round(w / 100) / 10;   // grams → kg, 1 dp
    const bd = typeof u.birthDate === "string" ? u.birthDate.slice(0, 4) : ""; if (/^\d{4}$/.test(bd)) patch.birth_year = Number(bd);
    const g = typeof u.gender === "string" ? u.gender.toLowerCase() : ""; if (g === "male" || g === "female") patch.sex = g;
    const v = Number(u.vo2MaxRunning); if (Number.isFinite(v) && v > 0) patch.vo2max = Math.round(v * 10) / 10;
    if (Object.keys(patch).length === 0) return NextResponse.json({ ok: false, error: "Garmin returned no profile fields." }, { status: 502 });
    const merged = await saveConfigKey(sql, "user_profile", patch);
    return NextResponse.json({ ok: true, profile: { weight_kg: merged.weight_kg ?? null, birth_year: merged.birth_year ?? null, sex: merged.sex ?? null, vo2max: merged.vo2max ?? null }, updated: Object.keys(patch) });
  } catch (err) {
    return NextResponse.json({ ok: false, error: `Garmin profile pull failed: ${err instanceof Error ? err.message : String(err)}` }, { status: 502 });
  }
}
