import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    rules: {
      // Flags every synchronous setState call at the top of a useEffect,
      // including the standard "reset local state, then resubscribe/refetch
      // for a new dependency" pattern this app uses throughout (lib/sse.ts's
      // stream hook, every merchant-picker-driven page, the gauge's stamp
      // timer) — a correct, idiomatic pattern, not a bug the rule is
      // designed to catch. Disabled deliberately rather than restructured
      // per call site.
      "react-hooks/set-state-in-effect": "off",
    },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
