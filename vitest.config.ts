import { defineConfig } from "vitest/config";

// Preact + Vitest harness (0a88d328). Component tests live alongside the
// components they cover as *.test.tsx and run in a jsdom DOM. JSX compiles to
// Preact's automatic runtime; react/react-dom alias to preact/compat so
// @testing-library/preact and any react-style imports resolve to Preact.
export default defineConfig({
  esbuild: {
    jsx: "automatic",
    jsxImportSource: "preact",
  },
  resolve: {
    alias: {
      react: "preact/compat",
      "react-dom": "preact/compat",
      "react/jsx-runtime": "preact/jsx-runtime",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["meridian/static/**/*.{test,spec}.{ts,tsx}"],
    coverage: {
      provider: "v8",
      // 8e29733e — frontend coverage gate: typed Preact components + the first
      // strict-typed legacy module. Types-only and test files carry no runtime
      // lines, so they're excluded from the denominator.
      include: [
        "meridian/static/components/**/*.{ts,tsx}",
        "meridian/static/dashboard-utils.ts",
        "meridian/static/dashboard-mcp.ts",
      ],
      exclude: ["**/*.test.{ts,tsx}", "**/types.ts"],
      thresholds: { lines: 70, functions: 70, branches: 70, statements: 70 },
    },
  },
});
