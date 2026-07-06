// Make @testing-library/jest-dom matchers (toBeInTheDocument, toHaveTextContent,
// …) visible to TypeScript across all Vitest component tests (ff8ff615). The
// runtime registration lives in vitest.setup.ts; this only loads the types.
import "@testing-library/jest-dom/vitest";
