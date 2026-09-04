import { defineConfig, configDefaults } from "vitest/config";
import path from "path";

// Minimal config: resolve the `@/` path alias (matches tsconfig) so route
// handlers and libs that import `@/lib/*` can be unit-tested. Test discovery
// and all other behaviour stay on the vitest defaults. `.test.ts` files next to
// the code they cover (e.g. lib/dedup.test.ts) are picked up automatically.
export default defineConfig({
  // Playwright specs live in e2e/ and are not vitest tests.
  test: { exclude: [...configDefaults.exclude, "e2e/**"] },
  resolve: {
    alias: { "@": path.resolve(__dirname, ".") },
  },
});
