import { getDb } from "./db";
import { getActivitiesByDate, type GarminActivity } from "./garmin-activities";
import { getGarminClient } from "./garmin-upload";

type Sql = ReturnType<typeof getDb>;

interface PlannedStep {
  kind?: string;
  name?: string;
  durationType?: string;
  durationValue?: number | string;
  targetType?: string;
  targetLow?: string | number;
  targetHigh?: string | number;
  previousSteps?: number | string;
  reps?: number | string;
}

export interface PlannedWorkout {
  garminWorkoutId: string;
  workoutTitle: string;
  planName?: string;
  sessionTitle?: string;
  description?: string;
  sport?: string;
  scheduledDate?: string | null;
  steps?: PlannedStep[];
}

export interface PlannedStravaResult {
  checked: number;
  updated: number;
  skipped: number;
  errors: string[];
}

interface StravaCredentials {
  client_id: string;
  client_secret: string;
  refresh_token: string;
}

interface StravaActivity {
  id: number;
  name?: string;
  sport_type?: string;
  type?: string;
  start_date?: string;
  start_date_local?: string;
  elapsed_time?: number;
}

const API_BASE = "https://www.strava.com/api/v3";

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function parseMs(value?: string | null): number | null {
  if (!value) return null;
  const iso = /[TZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value.replace(" ", "T")}Z`;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms;
}

function dateWindow(date: string): { after: number; before: number } {
  const start = Math.floor(Date.parse(`${date}T00:00:00Z`) / 1000) - 6 * 60 * 60;
  const end = Math.floor(Date.parse(`${date}T23:59:59Z`) / 1000) + 6 * 60 * 60;
  return { after: start, before: end };
}

function activityName(activity: GarminActivity): string {
  const a = activity as GarminActivity & { activityName?: string; name?: string };
  return String(a.activityName || a.name || "").trim();
}

function activitySport(activity: GarminActivity): string {
  const a = activity as GarminActivity & { activityType?: { typeKey?: string } };
  return String(a.activityType?.typeKey || "").toLowerCase();
}

function activityStart(activity: GarminActivity): string | null {
  return activity.startTimeGMT || activity.startTimeLocal || null;
}

function workoutIdOf(activity: GarminActivity): string {
  const a = activity as GarminActivity & { workoutId?: string | number; workoutName?: string };
  return String(a.workoutId || "").trim();
}

function isRun(activity: GarminActivity): boolean {
  const sport = activitySport(activity);
  return !sport || sport.includes("running") || sport === "run";
}

function matchesPlan(activity: GarminActivity, plan: PlannedWorkout): boolean {
  if (!isRun(activity)) return false;
  const wid = workoutIdOf(activity);
  if (wid && wid === plan.garminWorkoutId) return true;
  const name = activityName(activity).toLowerCase();
  const title = plan.workoutTitle.toLowerCase();
  return Boolean(name && title && (name === title || name.includes(title)));
}

function formatDuration(step: PlannedStep): string {
  if (step.durationType === "distance") {
    const metres = Number(step.durationValue);
    if (!Number.isFinite(metres) || metres <= 0) return "";
    return metres >= 1000
      ? `${(metres / 1000).toFixed(metres % 1000 ? 1 : 0)} km`
      : `${metres} m`;
  }
  if (step.durationType === "time") {
    const total = Number(step.durationValue);
    if (!Number.isFinite(total) || total <= 0) return "";
    const mins = Math.floor(total / 60);
    const secs = total % 60;
    return mins ? `${mins}:${String(secs).padStart(2, "0")}` : `${secs}s`;
  }
  return "lap button";
}

function formatTarget(step: PlannedStep): string {
  if (step.targetType === "pace") return ` @ ${step.targetLow || "?"}-${step.targetHigh || "?"}/km`;
  if (step.targetType === "heartRate") return ` @ ${step.targetLow || "?"}-${step.targetHigh || "?"} bpm`;
  return "";
}

export function plannedDescription(plan: PlannedWorkout): string {
  const lines = [
    plan.workoutTitle,
    "",
    "Structured planned workout",
  ];
  if (plan.description) lines.push("", plan.description);
  if (plan.steps?.length) {
    lines.push("");
    plan.steps.forEach((step, index) => {
      if (step.kind === "repeat") {
        lines.push(`Repeat: previous ${step.previousSteps || "?"} steps x ${step.reps || "?"}`);
        return;
      }
      const name = step.name || `Step ${index + 1}`;
      lines.push(`${name}: ${formatDuration(step)}${formatTarget(step)}`);
    });
  }
  lines.push("", "Synced from Garmin planned workout.");
  return lines.join("\n");
}

async function loadPlans(sql: Sql): Promise<PlannedWorkout[]> {
  const rows = await sql`SELECT value FROM app_cache WHERE key = 'planned_workouts' LIMIT 1`;
  const value = asObject(rows[0]?.value);
  const workouts = asObject(value.workouts);
  return Object.values(workouts)
    .map((raw) => asObject(raw))
    .filter((raw) => raw.garminWorkoutId && raw.workoutTitle && raw.scheduledDate)
    .map((raw) => ({
      garminWorkoutId: String(raw.garminWorkoutId),
      workoutTitle: String(raw.workoutTitle),
      planName: raw.planName ? String(raw.planName) : undefined,
      sessionTitle: raw.sessionTitle ? String(raw.sessionTitle) : undefined,
      description: raw.description ? String(raw.description) : undefined,
      sport: raw.sport ? String(raw.sport) : "running",
      scheduledDate: String(raw.scheduledDate),
      steps: Array.isArray(raw.steps) ? (raw.steps as PlannedStep[]) : [],
    }));
}

async function loadUpdated(sql: Sql): Promise<Record<string, unknown>> {
  const rows = await sql`SELECT value FROM app_cache WHERE key = 'planned_strava_updates' LIMIT 1`;
  return asObject(rows[0]?.value);
}

async function saveUpdated(sql: Sql, value: Record<string, unknown>): Promise<void> {
  await sql`
    INSERT INTO app_cache (key, value, updated_at)
    VALUES ('planned_strava_updates', ${sql.json(value)}, NOW())
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()`;
}

async function stravaCredentials(sql: Sql): Promise<StravaCredentials | null> {
  const rows = await sql`
    SELECT credentials FROM platform_credentials
    WHERE platform = 'strava' AND status = 'active'
    LIMIT 1`;
  const creds = asObject(rows[0]?.credentials);
  const client_id = String(creds.client_id || process.env.STRAVA_CLIENT_ID || "").trim();
  const client_secret = String(creds.client_secret || process.env.STRAVA_CLIENT_SECRET || "").trim();
  const refresh_token = String(creds.refresh_token || process.env.STRAVA_REFRESH_TOKEN || "").trim();
  return client_id && client_secret && refresh_token
    ? { client_id, client_secret, refresh_token }
    : null;
}

async function refreshStravaToken(sql: Sql, creds: StravaCredentials): Promise<string> {
  const response = await fetch("https://www.strava.com/oauth/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: creds.client_id,
      client_secret: creds.client_secret,
      refresh_token: creds.refresh_token,
      grant_type: "refresh_token",
    }),
  });
  if (!response.ok) throw new Error(`Strava token refresh failed (${response.status})`);
  const payload = asObject(await response.json());
  const token = String(payload.access_token || "");
  if (!token) throw new Error("Strava did not return an access token");
  const rotated = String(payload.refresh_token || "");
  if (rotated && rotated !== creds.refresh_token && !process.env.STRAVA_REFRESH_TOKEN) {
    await sql`
      UPDATE platform_credentials
      SET credentials = credentials || ${sql.json({ refresh_token: rotated })}, status = 'active'
      WHERE platform = 'strava'`;
  }
  return token;
}

async function stravaActivities(token: string, date: string): Promise<StravaActivity[]> {
  const { after, before } = dateWindow(date);
  const response = await fetch(
    `${API_BASE}/athlete/activities?after=${after}&before=${before}&per_page=50`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!response.ok) throw new Error(`Strava activity list failed (${response.status})`);
  const payload = await response.json();
  return Array.isArray(payload) ? payload as StravaActivity[] : [];
}

function matchingStravaActivity(
  activities: StravaActivity[],
  garminActivity: GarminActivity,
): StravaActivity | null {
  const start = parseMs(activityStart(garminActivity));
  if (start == null) return null;
  const matches = activities.filter((activity) => {
    const sport = String(activity.sport_type || activity.type || "").toLowerCase();
    if (sport && !sport.includes("run")) return false;
    const stravaStart = parseMs(activity.start_date || activity.start_date_local);
    return stravaStart != null && Math.abs(stravaStart - start) <= 10 * 60 * 1000;
  });
  return matches.length === 1 ? matches[0] : null;
}

async function updateStravaActivity(
  token: string,
  activityId: number,
  plan: PlannedWorkout,
): Promise<void> {
  const response = await fetch(`${API_BASE}/activities/${activityId}`, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      name: plan.workoutTitle,
      description: plannedDescription(plan),
    }),
  });
  if (!response.ok) throw new Error(`Strava activity update failed (${response.status})`);
}

export async function syncPlannedStrava(sql: Sql = getDb()): Promise<PlannedStravaResult> {
  const result: PlannedStravaResult = { checked: 0, updated: 0, skipped: 0, errors: [] };
  const plans = await loadPlans(sql);
  if (!plans.length) return result;
  const updated = await loadUpdated(sql);
  const creds = await stravaCredentials(sql);
  if (!creds) return { ...result, skipped: plans.length, errors: ["Strava credentials not configured"] };
  const token = await refreshStravaToken(sql, creds);
  const garmin = await getGarminClient();

  for (const plan of plans) {
    if (!plan.scheduledDate || updated[plan.garminWorkoutId]) {
      result.skipped += 1;
      continue;
    }
    result.checked += 1;
    try {
      const garminMatches = (await getActivitiesByDate(garmin, plan.scheduledDate))
        .filter((activity) => matchesPlan(activity, plan));
      if (garminMatches.length !== 1) {
        result.skipped += 1;
        continue;
      }
      const strava = matchingStravaActivity(
        await stravaActivities(token, plan.scheduledDate),
        garminMatches[0],
      );
      if (!strava) {
        result.skipped += 1;
        continue;
      }
      await updateStravaActivity(token, strava.id, plan);
      updated[plan.garminWorkoutId] = {
        stravaActivityId: strava.id,
        garminActivityId: garminMatches[0].activityId ?? null,
        workoutTitle: plan.workoutTitle,
        updatedAt: new Date().toISOString(),
      };
      await saveUpdated(sql, updated);
      result.updated += 1;
    } catch (error) {
      result.errors.push(`${plan.workoutTitle}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  return result;
}
