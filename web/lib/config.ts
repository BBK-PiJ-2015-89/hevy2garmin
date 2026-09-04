import type { getDb } from "./db";
type Sql = ReturnType<typeof getDb>;
const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** Merge `patch` into the app_cache config `key` (the Python `save_config` shape): existing keys are kept, patched keys win. */
export async function saveConfigKey(sql: Sql, key: string, patch: Record<string, unknown>): Promise<Record<string, unknown>> {
  const rows = (await sql`SELECT value FROM app_cache WHERE key = ${key} LIMIT 1`) as { value: unknown }[];
  const merged = { ...(isObj(rows[0]?.value) ? rows[0].value : {}), ...patch };
  await sql`
    INSERT INTO app_cache (key, value)
    VALUES (${key}, ${sql.json(merged)})
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()`;
  return merged;
}
