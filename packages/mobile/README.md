# TRINDATRON MOBILE

Universal client (Android, iPhone, iPad and Web) for TRINITY CORE — Expo SDK 57, React Native 0.86, Expo Router, TypeScript.

The app presents; the Core decides. No model, provider, TCU, entitlement, billing, agent or memory rule lives here,
and no secret goes into the bundle — only the Core's public URL and the user's own session token.

```bash
npm install
cp .env.example .env     # EXPO_PUBLIC_API_URL
npm run web              # or: npm run android / npm run ios
```

| | |
|---|---|
| `npm run typecheck` | TypeScript |
| `npm run lint` | ESLint (Expo) |
| `npm test` | Unit tests |
| `CORE_INTEGRATION_URL=http://127.0.0.1:8000 npm run test:integration` | Tests against a real Core |
| `npm run export:web` | Production Web build |

Full documentation (in Portuguese for now) — architecture, Core contracts, session, streaming, platforms, EAS and
known blockers: [`docs/MOBILE.md`](../../docs/MOBILE.md).
