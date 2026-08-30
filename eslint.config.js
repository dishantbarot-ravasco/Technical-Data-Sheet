// eslint.config.js — Frontend JS lint config (flat config format, ESLint 9+).
//
// Scope: frontend/js/*.js only — the standalone ES modules (api.js, auth.js,
// generate-tds.js, search-tds.js). Several pages also carry their own inline
// <script type="module"> blocks (admin.html, tds-multi-preview.html, etc.)
// which aren't covered here; linting those would need extracting them from
// HTML first, which is a separate piece of work. This intentionally uses
// only ESLint's built-in recommended rules — no extra plugins/config to
// verify — and CI runs it non-blocking (see .github/workflows/ci.yml) since
// it could not be tested against a real Node install while authoring it.
import js from '@eslint/js';

export default [
  js.configs.recommended,
  {
    files: ['frontend/js/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        window: 'readonly',
        document: 'readonly',
        fetch: 'readonly',
        sessionStorage: 'readonly',
        localStorage: 'readonly',
        console: 'readonly',
        URLSearchParams: 'readonly',
        FormData: 'readonly',
      },
    },
    rules: {
      'no-unused-vars': 'warn',
    },
  },
];
