import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { getDb } from "@/lib/db";
import { saveGithubPat } from "@/lib/github";
import { saveConfigKey } from "@/lib/config";
import { verifySession, SESSION_COOKIE, authEnabled } from "@/lib/auth";

// Writes config to the live hevy2garmin Postgres (app_cache) at request time.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/**
 * POST /api/settings
 * Body: a partial map of editable config keys, e.g.
 *   { auto_sync: { enabled, interval_minutes },
 *     hr_fusion: { enabled },
 *     merge_settings: { merge_watch_strategy, merge_activity_types },
 *     user_profile: { weight_kg } }
 *
 * Session-gated. For each provided key it sanitises the payload, deep-merges it
 * onto the stored value (preserving sub-fields the form doesn't manage), and
 * upserts into app_cache — matching PostgresDatabase.set_cache (db_postgres.py:499).
 * Config only; no sync/upload side effects.
 */

const INTERVALS = [30, 60, 120, 240, 360, 720, 1440];
const WATCH_STRATEGIES = ["replace", "merge", "describe"];

type Obj = Record<string, unknown>;

function isObj(v: unknown): v is Obj {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Clamp an integer to [lo, hi], or undefined when not a finite number. */
function clampInt(v: unknown, lo: number, hi: number): number | undefined {
  const n = Math.round(Number(v));
  if (!Number.isFinite(n)) return undefined;
  return Math.max(lo, Math.min(hi, n));
}

/**
 * Keep only the recognised, validated sub-fields for a given config key. Ranges
 * mirror the Python POST /settings handler (settings_save).
 */
function sanitise(key: string, raw: Obj): Obj {
  const out: Obj = {};
  if (key === "auto_sync") {
    if ("enabled" in raw) out.enabled = Boolean(raw.enabled);
    if ("interval_minutes" in raw) {
      const n = Number(raw.interval_minutes);
      out.interval_minutes = INTERVALS.includes(n) ? n : 120;
    }
  } else if (key === "hr_fusion") {
    if ("enabled" in raw) out.enabled = Boolean(raw.enabled);
  } else if (key === "strava_settings") {
    if ("visual_strength_upload" in raw) {
      out.visual_strength_upload = Boolean(raw.visual_strength_upload);
    }
  } else if (key === "merge_settings") {
    if ("merge_watch_strategy" in raw) {
      const s = String(raw.merge_watch_strategy);
      out.merge_watch_strategy = WATCH_STRATEGIES.includes(s) ? s : "merge";
    }
    if ("merge_mode" in raw) out.merge_mode = Boolean(raw.merge_mode);
    if ("description_enabled" in raw) out.description_enabled = Boolean(raw.description_enabled);
    if ("merge_overlap_pct" in raw) {
      const n = clampInt(raw.merge_overlap_pct, 50, 95);
      if (n !== undefined) out.merge_overlap_pct = n;
    }
    if ("merge_max_drift_min" in raw) {
      const n = clampInt(raw.merge_max_drift_min, 5, 60);
      if (n !== undefined) out.merge_max_drift_min = n;
    }
    if (Array.isArray(raw.merge_activity_types)) {
      // Always keep strength_training first, dedupe, drop blanks (matches Python).
      const extras = (raw.merge_activity_types as unknown[])
        .map((t) => String(t).trim().toLowerCase().replace(/\s+/g, "_"))
        .filter((t) => t && t !== "strength_training");
      out.merge_activity_types = ["strength_training", ...Array.from(new Set(extras))];
    }
  } else if (key === "user_profile") {
    if ("weight_kg" in raw) {
      const w = Number(raw.weight_kg);
      if (Number.isFinite(w) && w > 0 && w < 500) out.weight_kg = Math.round(w * 10) / 10;
    }
    if ("birth_year" in raw) {
      const n = clampInt(raw.birth_year, 1900, 2025);
      if (n !== undefined) out.birth_year = n;
    }
    if ("sex" in raw) {
      const s = String(raw.sex).toLowerCase();
      if (s === "male" || s === "female") out.sex = s;
    }
    if ("vo2max" in raw) {
      const v = Number(raw.vo2max);
      if (Number.isFinite(v) && v > 0 && v < 100) out.vo2max = Math.round(v * 10) / 10;
    }
    if ("timezone" in raw) out.timezone = String(raw.timezone).trim();
  } else if (key === "timing") {
    for (const [field, [lo, hi]] of Object.entries({
      working_set_seconds: [1, 3600],
      warmup_set_seconds: [1, 3600],
      rest_between_sets_seconds: [0, 3600],
      rest_between_exercises_seconds: [0, 3600],
    }) as [string, [number, number]][]) {
      if (field in raw) {
        const n = clampInt(raw[field], lo, hi);
        if (n !== undefined) out[field] = n;
      }
    }
  }
  return out;
}

const EDITABLE = ["auto_sync", "hr_fusion", "strava_settings", "merge_settings", "user_profile", "timing"];

async function saveStravaCredentials(sql: ReturnType<typeof getDb>, raw: Obj): Promise<boolean> {
  const updates: Record<string, string> = {};
  for (const [bodyKey, credKey] of [
    ["strava_client_id", "client_id"],
    ["strava_client_secret", "client_secret"],
    ["strava_refresh_token", "refresh_token"],
  ] as const) {
    const value = typeof raw[bodyKey] === "string" ? raw[bodyKey].trim() : "";
    if (value) updates[credKey] = value;
  }
  if (Object.keys(updates).length === 0) return false;

  const rows = (await sql`
    SELECT credentials
    FROM platform_credentials
    WHERE platform = 'strava'
    LIMIT 1
  `) as { credentials: unknown }[];
  const current = isObj(rows[0]?.credentials) ? rows[0].credentials : {};
  const merged = { ...current, ...updates };
  const active = ["client_id", "client_secret", "refresh_token"].every((k) => {
    const value = merged[k];
    return typeof value === "string" && value.trim().length > 0;
  });
  await sql`
    INSERT INTO platform_credentials (platform, auth_type, credentials, status, connected_at)
    VALUES ('strava', 'oauth', ${sql.json(merged)}, ${active ? "active" : "disconnected"}, NOW())
    ON CONFLICT (platform) DO UPDATE
       SET credentials = EXCLUDED.credentials,
           status = EXCLUDED.status`;
  return true;
}

export async function POST(request: Request) {
  // Gate only when a password is configured (prod). With no password set the app
  // is open, matching the login model and the other benign DB-write routes.
  if (authEnabled()) {
    const store = await cookies();
    if (!(await verifySession(store.get(SESSION_COOKIE)?.value ?? null))) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
  }

  let body: Obj;
  try {
    body = (await request.json()) as Obj;
  } catch {
    return NextResponse.json({ error: "Invalid JSON body." }, { status: 400 });
  }

  const changes: Record<string, Obj> = {};
  for (const key of EDITABLE) {
    if (isObj(body[key])) {
      const s = sanitise(key, body[key] as Obj);
      if (Object.keys(s).length > 0) changes[key] = s;
    }
  }
  // GitHub token (#458): not app_cache config — the platform_credentials row the Python
  // dashboard writes from its Settings page. Blank = keep the current one.
  const githubPat = typeof body.github_pat === "string" ? body.github_pat.trim() : "";
  const hasStravaCreds = ["strava_client_id", "strava_client_secret", "strava_refresh_token"].some(
    (k) => typeof body[k] === "string" && body[k].trim().length > 0,
  );
  const keys = Object.keys(changes);
  if (keys.length === 0 && !githubPat && !hasStravaCreds) {
    return NextResponse.json({ error: "No editable config provided." }, { status: 400 });
  }

  let sql: ReturnType<typeof getDb>;
  try {
    sql = getDb();
  } catch {
    return NextResponse.json({ error: "DATABASE_URL not configured." }, { status: 503 });
  }

  try {
    if (githubPat) await saveGithubPat(sql, githubPat);
    const savedStravaCreds = await saveStravaCredentials(sql, body);
    for (const key of keys) await saveConfigKey(sql, key, changes[key]);
    return NextResponse.json({
      ok: true,
      saved: [
        ...keys,
        ...(githubPat ? ["github_pat"] : []),
        ...(savedStravaCreds ? ["strava_credentials"] : []),
      ],
    });
  } catch (err) {
    console.error("settings write failed:", err);
    return NextResponse.json({ error: "Failed to save settings." }, { status: 500 });
  }
}
