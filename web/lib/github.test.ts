import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import sodium from "libsodium-wrappers";
import { minutesToCron, formatIntervalLabel, buildSyncWorkflowYaml, sealSecret, setupGithubActions, disableGithubActions } from "./github";

describe("cron + labels (parity with the Python dashboard)", () => {
  it("maps the dashboard's intervals", () => {
    expect(minutesToCron(30)).toBe("*/30 * * * *");
    expect(minutesToCron(60)).toBe("0 * * * *");
    expect(minutesToCron(120)).toBe("0 */2 * * *");
    expect(minutesToCron(240)).toBe("0 */4 * * *");
    expect(minutesToCron(1440)).toBe("0 0 * * *");
    expect(minutesToCron(45)).toBe("0 */2 * * *"); // unexpected → default
  });
  it("labels", () => {
    expect(formatIntervalLabel(30)).toBe("30 minutes");
    expect(formatIntervalLabel(60)).toBe("1 hour");
    expect(formatIntervalLabel(120)).toBe("2 hours");
    expect(formatIntervalLabel(1440)).toBe("24 hours");
  });
});

describe("sync workflow YAML", () => {
  it("is byte-for-byte what the Python dashboard writes (tests/fixtures/sync_workflow_120.yml)", () => {
    const golden = readFileSync(new URL("../../tests/fixtures/sync_workflow_120.yml", import.meta.url), "utf8");
    expect(buildSyncWorkflowYaml(120)).toBe(golden);
  });
});

describe("sealSecret", () => {
  it("produces a sealed box the repo's private key can open", async () => {
    await sodium.ready;
    const kp = sodium.crypto_box_keypair();
    const pkB64 = sodium.to_base64(kp.publicKey, sodium.base64_variants.ORIGINAL);
    const sealed = await sealSecret(pkB64, "postgres://u:p@h/db");
    const opened = sodium.crypto_box_seal_open(sodium.from_base64(sealed, sodium.base64_variants.ORIGINAL), kp.publicKey, kp.privateKey);
    expect(sodium.to_string(opened)).toBe("postgres://u:p@h/db");
  });
});

function fakeGithub(overrides: Partial<Record<string, (init?: RequestInit) => Response>> = {}) {
  const calls: { method: string; path: string; body?: unknown }[] = [];
  const kp = { pub: "" };
  const fetchImpl = (async (url: string | URL, init?: RequestInit) => {
    const u = String(url); const path = u.replace("https://api.github.com/repos/", ""); const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const key = `${method} ${path}`;
    if (overrides[key]) return overrides[key]!(init);
    if (key === "PATCH o/r") return new Response("{}", { status: 200 });
    if (key === "PUT o/r/actions/permissions") return new Response(null, { status: 204 });
    if (key === "GET o/r/actions/secrets/public-key") return new Response(JSON.stringify({ key: kp.pub, key_id: "k1" }), { status: 200 });
    if (key === "GET o/r/contents/.github/workflows/sync.yml") return new Response(JSON.stringify({ sha: "abc" }), { status: 200 });
    if (key === "PUT o/r/actions/secrets/DATABASE_URL") return new Response(null, { status: 201 });
    if (key === "PUT o/r/contents/.github/workflows/sync.yml") return new Response("{}", { status: 200 });
    if (key === "POST o/r/dispatches") return new Response(null, { status: 204 });
    if (key === "DELETE o/r/contents/.github/workflows/sync.yml") return new Response("{}", { status: 200 });
    return new Response("nope", { status: 404 });
  }) as unknown as typeof fetch;
  return { calls, fetchImpl, kp };
}

describe("setupGithubActions", () => {
  it("runs the Python sequence: public + permissions + key + workflow, then secret + workflow (with sha), then dispatch", async () => {
    await sodium.ready; const { calls, fetchImpl, kp } = fakeGithub(); kp.pub = sodium.to_base64(sodium.crypto_box_keypair().publicKey, sodium.base64_variants.ORIGINAL);
    const r = await setupGithubActions({ pat: "t", repo: "o/r", databaseUrl: "postgres://x", intervalMinutes: 240, fetchImpl });
    expect(r).toEqual({ ok: true, message: "Auto-sync enabled! Workouts will sync every 4 hours." });
    await new Promise((res) => setTimeout(res, 10));
    const seq = calls.map((c) => `${c.method} ${c.path}`);
    expect(seq.slice(0, 4).sort()).toEqual(["GET o/r/actions/secrets/public-key", "GET o/r/contents/.github/workflows/sync.yml", "PATCH o/r", "PUT o/r/actions/permissions"].sort());
    expect(seq.slice(4, 6).sort()).toEqual(["PUT o/r/actions/secrets/DATABASE_URL", "PUT o/r/contents/.github/workflows/sync.yml"].sort());
    expect(seq[6]).toBe("POST o/r/dispatches");
    const wf = calls.find((c) => c.method === "PUT" && c.path.endsWith("sync.yml"))!.body as { sha: string; content: string; message: string };
    expect(wf.sha).toBe("abc"); expect(wf.message).toBe("feat: auto-sync every 4 hours");
    expect(Buffer.from(wf.content, "base64").toString("utf8")).toBe(buildSyncWorkflowYaml(240));
    const secret = calls.find((c) => c.path.endsWith("secrets/DATABASE_URL"))!.body as { key_id: string; encrypted_value: string };
    expect(secret.key_id).toBe("k1"); expect(secret.encrypted_value.length).toBeGreaterThan(20);
  });
  it("reports a failed permissions call the way Python does", async () => {
    await sodium.ready; const { fetchImpl, kp } = fakeGithub({ "PUT o/r/actions/permissions": () => new Response("x", { status: 500 }) }); kp.pub = sodium.to_base64(sodium.crypto_box_keypair().publicKey, sodium.base64_variants.ORIGINAL);
    expect(await setupGithubActions({ pat: "t", repo: "o/r", databaseUrl: "postgres://x", fetchImpl })).toEqual({ ok: false, message: "Failed to enable Actions: HTTP 500" });
  });
  it("disable deletes the workflow with its sha", async () => {
    const { calls, fetchImpl } = fakeGithub();
    expect(await disableGithubActions({ pat: "t", repo: "o/r", fetchImpl })).toBe(true);
    const del = calls.find((c) => c.method === "DELETE")!; expect((del.body as { sha: string }).sha).toBe("abc");
  });
});
