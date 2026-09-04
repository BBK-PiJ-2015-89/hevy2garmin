import { test, expect } from "@playwright/test";

test.describe("dashboard smoke (no database, password auth)", () => {
  test("gated pages redirect to login, login works, pages render, /sync redirects", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page).toHaveURL(/\/login/);
    await page.getByTestId("login-password").fill("test-pw");
    await page.getByTestId("login-submit").click();
    await expect(page).toHaveURL(/\/dashboard/);
    await expect(page.locator("h1").first()).toBeVisible();
    for (const path of ["/setup", "/routines", "/workouts", "/settings"]) {
      const res = await page.goto(path);
      expect(res?.status(), `${path} status`).toBeLessThan(500);
      await expect(page.locator("h1").first()).toBeVisible();
    }
    await page.goto("/sync");
    await expect(page).toHaveURL(/\/dashboard/);
  });
  test("a wrong password stays on login with an error", async ({ page }) => {
    await page.goto("/login");
    await page.getByTestId("login-password").fill("nope");
    await page.getByTestId("login-submit").click();
    await expect(page.getByText("Incorrect password")).toBeVisible();   // (role=alert is shared with Next's route announcer)
    await expect(page).toHaveURL(/\/login/);
  });
});
