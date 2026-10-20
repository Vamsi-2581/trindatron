# TRINITY.AI — Mobile Architecture Decision

**Data:** 2026-08-24  
**Status:** APROVADO (decisão arquitetural, sem implementação nesta fase)  
**Autor:** Diego  
**Fase de referência:** G (pós-G-4, pré-G-5)

---

## 1. Contexto e objetivo

TRINITY.AI será lançado como plataforma multi-cliente: API backend (Core), aplicação desktop
(Electron), aplicação web (Next.js) e, futuramente, aplicativo móvel nativo (iOS + Android).

Este documento registra a decisão de arquitetura mobile **antes** da implementação, para garantir
que as fases em andamento (G-5 em diante) não criem dívidas técnicas que dificultem o cliente
móvel.

A análise cobre 10 áreas de compatibilidade. Nenhuma modificação de código foi necessária —
a API atual é mobile-ready.

---

## 2. Auditoria de compatibilidade — 10 áreas

### Área 1: Compatibilidade da API REST com clientes móveis

**Veredicto: ✅ Totalmente compatível**

| Aspecto | Status |
|---|---|
| Protocolo | HTTP/1.1 REST + JSON — padrão universal |
| Autenticação | `Authorization: Bearer <token>` — compatível com todos os frameworks |
| Respostas | JSON com Pydantic schemas — tipagem segura |
| CORS | `allowed_origins: list[str]` configurável via env var (`ALLOWED_ORIGINS`) |
| Erros | HTTP status codes padrão (401, 404, 422, 429) |
| Paginação | `SessionListResponse` retorna `total` — pronto para listas |

**CORS em produção:** `config.py:54` default `["http://localhost:3000", "http://localhost:3001"]`.
Para mobile nativo, CORS não se aplica (cliente não é browser). Para PWA, adicionar domínio
de produção. O validator de produção (`config.py:116`) rejeita `localhost` em prod —
comportamento correto.

---

### Área 2: Autenticação JWT para mobile

**Veredicto: ✅ Compatível — Bearer é o padrão correto para mobile**

```
POST /auth/register  →  { access_token, user_id, email }
POST /auth/login     →  { access_token, user_id, email }
GET  /auth/me        →  { id, email, full_name, plan, is_active, created_at }
```

- **Algoritmo:** HS256 (via `python-jose`) — `config.py:44`
- **Expiração:** `jwt_expiration_minutes: int = 60 * 24` (24 horas) — adequado para mobile
- **Armazenamento no mobile:**
  - React Native: `expo-secure-store` (iOS Keychain / Android Keystore) — **recomendado**
  - Flutter: `flutter_secure_storage`
  - PWA: `localStorage` (mesmo padrão do desktop/web atual)

**Limitação identificada (não bloqueante):** Sem refresh token — sessão expira em 24h.
Workaround aceitável para MVP: re-login automático quando 401. Refresh tokens são Fase 8+.

**GET /auth/me após login:** O desktop tem bug de `plan` hardcoded (corrigido no web).
No mobile, sempre chamar `GET /auth/me` após login para obter o plano real.

---

### Área 3: SSE (Server-Sent Events) no Android e iOS

**Veredicto: ⚠️ Compatível com polyfill — sem bloqueio arquitetural**

O Core usa SSE via `StreamingResponse` com `media_type="text/event-stream"` para:
- `POST /sessions/{id}/chat` — streaming de chat
- `POST /sessions/{id}/agent` — streaming de agente
- `POST /workspaces/code/{id}/execute` — streaming de execução de código

**Compatibilidade por plataforma:**

| Plataforma | `EventSource` nativo | `ReadableStream.getReader()` | Solução |
|---|---|---|---|
| iOS Safari (web/PWA) | ✅ desde iOS 9 | ✅ | Nativo |
| Android Chrome (web/PWA) | ✅ desde 2012 | ✅ | Nativo |
| React Native | ❌ ausente | ❌ ausente | Polyfill necessário |
| Flutter | ❌ ausente | N/A (Dart) | `http` package com `Stream` |
| Expo (managed) | ❌ ausente | ❌ ausente | Polyfill necessário |

**Polyfill para React Native:** `react-native-sse` (NPM, 1.5k stars) implementa
`EventSource` conforme especificação W3C. Alternativa: parser manual sobre
`fetch` com `ReadableStream` via polyfill `react-native-fetch-api`.

**Formato SSE atual (sem mudança necessária no Core):**
```
data: {"type":"text","content":"..."}\n\n
data: {"type":"done","session_id":"...","usage":{...}}\n\n
data: [DONE]\n\n
```

O formato `data: JSON\n\n` é o padrão W3C — polyfills leem corretamente.

---

### Área 4: TRINITY CODE em mobile

**Veredicto: ✅ Sem bloqueador — mobile é cliente puro**

`POST /workspaces/code/{workspace_id}/execute` (`workspaces.py:98`) retorna SSE.

- **Docker:** roda no servidor, não no dispositivo móvel — zero impacto
- **Mobile flow:** usuário digita comando → request JSON → SSE stream de output
- **Timeout:** `CodeRunRequest.timeout` validado no schema (ge=1, le=120) — mobile respeitará
- **Feedback visual:** eventos `tool_status` → `output` → `done` mapeiam naturalmente
  para uma UI de terminal embutida no mobile

**UX recommendation:** usar fonte monospace (`JetBrains Mono` / `Courier New`) para output.

---

### Área 5: TRINITY WORK — limitações em mobile

**Veredicto: ⚠️ Limitação identificada — sem bloqueio para G-5**

WORK workspace tem campo `requires_approval` / `approval_policy` (WorkWorkspace model).
Fluxo de aprovação planejado: agente solicita ação → humano aprova/rejeita.

**Gap para mobile:**
- Sem mecanismo de push notification quando há ação pendente de aprovação
- Mobile precisaria de polling ou WebSocket (não implementado) para receber alertas
- O endpoint de aprovação ainda não existe (Fase G+)

**Recomendação:** quando implementar TRINITY WORK em mobile, adicionar webhook/FCM
(Firebase Cloud Messaging) no backend para disparar push notifications.
Este gap não afeta G-5 (AuditLog/PermissionGateway).

---

### Área 6: Comparação de frameworks mobile

| Framework | Reuso de código | Performance | SSE | Maturidade | Ecossistema |
|---|---|---|---|---|---|
| **React Native + Expo** | ✅✅ Alto (TS + React) | ✅ Boa | Polyfill simples | ✅✅ Maduro | ✅✅ Grande |
| Flutter | ❌ Baixo (Dart) | ✅✅ Excelente | Dart Stream | ✅✅ Maduro | ✅ Crescendo |
| Capacitor (wrapper) | ✅✅ Máximo (web=mobile) | ⚠️ WebView | ✅ Nativo | ✅ Maduro | Médio |
| PWA | ✅✅ Máximo (web=mobile) | ⚠️ Limitações iOS | ✅ Nativo | ✅ Estável | N/A |

**Análise de custos de reuso:**

TRINITY já possui TypeScript + React + Zustand + tipos compartilhados (`types.ts`).
Com React Native + Expo:
- `types.ts` → reuso 100%
- `useAppStore.ts` → reuso ~90% (trocar localStorage por AsyncStorage)
- `api/client.ts` → reuso ~85% (trocar `ReadableStream` por polyfill SSE)
- UI components → reuso ~30% (adaptar Tailwind para StyleSheet ou NativeWind)

---

### Área 7: Estrutura de monorepo proposta

```
packages/
├── core/              (FastAPI — Python)
├── agent/             (Agent executor — Python)
├── model-router/      (Model selector — Python)
├── sandbox/           (Docker sandbox — Python)
├── desktop/           (Electron + React — TypeScript)
├── web/               (Next.js 14 — TypeScript)
└── mobile/            (Expo + React Native — TypeScript)  ← Fase futura
    ├── app/
    │   ├── _layout.tsx
    │   ├── (auth)/
    │   │   └── login.tsx
    │   └── (app)/
    │       ├── chat/
    │       ├── code/
    │       └── settings/
    ├── src/
    │   ├── api/client.ts     (baseado em packages/web/src/api/client.ts)
    │   ├── store/            (Zustand, igual ao web)
    │   └── types.ts          (cópia de packages/web/src/types.ts)
    ├── package.json
    └── app.json
```

**Dependências a adicionar em `packages/mobile`:**
```json
{
  "expo": "~51",
  "react-native": "0.74",
  "expo-router": "~3",
  "expo-secure-store": "~13",
  "react-native-sse": "^1.0.0",
  "zustand": "^5"
}
```

**Não é necessário modificar `packages/core/`.**

---

### Área 8: Princípios arquiteturais para compatibilidade mobile

Os seguintes princípios do Core garantem compatibilidade mobile sem modificação:

1. **API-first:** backend completamente agnóstico de cliente (sem server-side rendering)
2. **Bearer JWT stateless:** mobile não depende de cookies ou sessões de servidor
3. **JSON schemas validados (Pydantic):** clientes podem gerar tipos automaticamente
4. **SSE format W3C:** `data: JSON\n\n` — polyfills implementam fielmente
5. **CORS configurável:** `ALLOWED_ORIGINS` env var — sem `*` em produção
6. **HTTP status semânticos:** 401/403/404/422/429 — mobile trata igual ao web

**Princípio a reforçar em fases futuras:** nunca adicionar lógica que assuma
`window`, `localStorage`, `document` ou DOM no backend.

---

### Área 9: Impacto nas fases futuras

| Fase | Impacto em mobile | Ação necessária |
|---|---|---|
| G-5 (AuditLog + PermissionGateway) | Nenhum — backend only | Nada |
| Fase 7 (Web MVP) | Base de código compartilhável | Manter `types.ts` sincronizado |
| Fase 8 (Billing) | Payment sheet mobile-specific | Backend agnóstico (webhook) |
| Fase 9 (Memory) | Agnóstico de cliente | Nada |
| Fase Mobile | Implementar `packages/mobile` | Ver estrutura Área 7 |

**Push notifications (WORK approval):** quando implementar aprovação de ações do agente,
adicionar `device_token` ao User model e integração com FCM/APNs. Backend fica com endpoint
`POST /users/me/device-token`.

---

### Área 10: Conclusão e decisão

**Decisão arquitetural:** **React Native com Expo**

**Razão:** máximo reuso de código TypeScript, React, Zustand e tipos já existentes.
A curva de aprendizado é mínima dado que o time já trabalha com React no desktop e web.

**Quando implementar:** após Fase 7 (Web MVP). O web MVP valida a API client layer
e a store — o mobile herda essa base testada.

**Pré-requisitos antes de iniciar o mobile:**
- [ ] `packages/web/` estável e com testes passando
- [ ] `types.ts` sincronizado entre `web/` e `mobile/`
- [ ] API base URL configurável via env (`EXPO_PUBLIC_API_URL`)
- [ ] `expo-secure-store` para JWT (não localStorage)
- [ ] Polyfill SSE: `react-native-sse`

**Nenhum arquivo de backend precisa ser modificado para suportar mobile.**

---

## 3. Problemas críticos encontrados

**Nenhum.** A API atual é mobile-ready sem modificações.

Limitações documentadas (não bloqueantes para G-5):
- SSE requer polyfill em React Native (decisão de implementação, não arquitetural)
- Sem refresh token (aceitável para MVP, Fase 8+)
- WORK approval sem push notifications (Fase futura)

---

## 4. Estado dos testes (confirmado)

Auditoria realizada com base em leitura de código — zero modificações feitas.
Testes executados após auditoria: **385 passed, 3 skipped, 0 failed**.

Arquivos inspecionados:
- `packages/core/src/main.py` — CORS middleware
- `packages/core/src/config.py` — `allowed_origins`, JWT settings
- `packages/core/src/services/auth_service.py` — JWT criação/decode
- `packages/core/src/schemas/auth.py` — schemas de auth
- `packages/core/src/api/routes/auth.py` — endpoints de auth
- `packages/core/src/api/routes/sessions.py` — SSE chat/agent
- `packages/core/src/api/routes/workspaces.py` — SSE code execute
- `packages/core/src/services/code_service.py` — stream_code_execute
- `packages/core/src/schemas/code.py` — CodeRunRequest
- `packages/desktop/src/renderer/src/api/client.ts` — padrão SSE ReadableStream
- `packages/desktop/src/renderer/src/types.ts` — tipos compartilháveis
