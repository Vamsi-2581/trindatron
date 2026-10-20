# TRINDATRON MOBILE

Um único código (Expo + React Native + Expo Router + TypeScript) para **Android, iPhone, iPad e Web**.
O Mobile é uma nova interface para o **mesmo TRINITY CORE** — não é um segundo TRINDATRON.

```
Android · iOS · iPad · Web
        ↓
TRINDATRON MOBILE  (packages/mobile — apresenta)
        ↓  HTTPS · JSON · SSE · Bearer JWT
TRINITY CORE       (packages/core — decide)
Auth · Chat · Memory · Projects · Files · Agents · Billing · Entitlements · Model Router · Tools · Providers
```

O Mobile **não** contém: seleção de modelo ou provider, TCU, entitlements, regras de preço ou cobrança,
runtime de agentes, lógica de memória, permissões ou política de segurança. Tudo isso é consumido do Core.
O aplicativo conhece apenas a URL pública do Core e o token do próprio usuário.

---

## 1. Stack e versões

| Item | Versão | Por quê |
|---|---|---|
| Expo SDK | 57 (`expo ~57.0.22`) | Linha estável atual (lançada em 30/06/2026, 22 patches). Nada de canary/preview. |
| React Native | 0.86.3 | Fixada pelo SDK 57. |
| React | 19.2.3 | Fixada pelo SDK 57. |
| Expo Router | ~57.0.21 | Navegação por arquivos em Android, iOS e Web. Sem React Navigation direto. |
| TypeScript | ~6.0.3 | Versão do template oficial do SDK 57. |
| Node | ≥ 20.19 | Requisito do Expo. |

As dependências nativas seguem o `bundledNativeModules.json` do SDK 57, com uma exceção deliberada:
`react-native-gesture-handler` fica fixado em **3.3.0**, e o `npx expo install --check` sugere `~2.32.0`. A 3.3.0
está no `package-lock.json` desde a criação do app (vinha pelo `expo-router`), então todo development build já
foi gerado com ela. Descer para a 2.32 trocaria o código nativo e exigiria um build novo. Ela virou dependência
direta quando a lista da conversa passou a usar a biblioteca de chat (ver §6).

Dependências de terceiros, cada uma com motivo:

| Pacote | Motivo |
|---|---|
| `expo/fetch` (do próprio Expo) | Corpo de resposta em *stream* em Android/iOS/Web → lê o SSE do Core sem polyfill. |
| `expo-secure-store` | Token de sessão no Keychain (iOS) / Keystore (Android). |
| `react-native-keyboard-controller` | Teclado consistente com Android edge-to-edge (obrigatório no SDK 57) e iOS. |
| `@kesha-antonov/react-native-chat` (5.0.1) + `react-native-gesture-handler` | Estrutura da lista da conversa: linhas que só re-renderizam quando os próprios dados mudam, separadores de dia e teclado medido. Balões, composer e banners continuam nossos (§6). |
| `marked` | Apenas tokeniza Markdown; a renderização é nativa (sem HTML, sem imagens remotas). |
| `zustand` | Estado de sessão (≈1 kB), o mesmo padrão do Web. |
| `@react-native-async-storage/async-storage`, `@react-native-community/netinfo` | Cache offline mínimo e estado de rede. |
| `expo-audio`, `expo-image-picker`, `expo-document-picker`, `expo-clipboard`, `expo-web-browser`, `expo-image` | Voz, câmera/galeria, documentos, copiar, checkout hospedado, imagens. |

`packages/mobile` **não** faz parte dos workspaces npm da raiz: o Web usa React 18 e o Mobile React 19, e o
*hoisting* do npm misturaria as duas versões. O pacote tem o próprio `package-lock.json`.

---

## 2. Instalação e desenvolvimento

```bash
cd packages/mobile
npm install
cp .env.example .env          # ajuste EXPO_PUBLIC_API_URL
npm run web                   # navegador
npm run android               # emulador/dispositivo Android (Expo Go não cobre módulos nativos → use development build)
npm run ios                   # simulador iOS (macOS)
```

Core local para desenvolvimento: `packages/core` (FastAPI) em `http://localhost:8000`.
Para o app **Web** acessar o Core, a origem precisa estar em `ALLOWED_ORIGINS` do Core
(ex.: `http://localhost:8081`). Apps nativos não usam CORS.

| Onde roda o app | `EXPO_PUBLIC_API_URL` |
|---|---|
| Web / simulador iOS na mesma máquina | `http://localhost:8000` |
| Emulador Android | `http://10.0.2.2:8000` |
| Dispositivo físico na rede | `http://<IP-do-computador>:8000` |

Sem `EXPO_PUBLIC_API_URL`, o modo desenvolvimento usa os padrões acima; **um build de release sem a variável
mostra uma tela de configuração ausente** em vez de adivinhar um servidor.

> `EXPO_PUBLIC_*` é embutido no bundle e é legível por qualquer pessoa. Nunca coloque segredos ali.

Scripts: `npm run typecheck`, `npm run lint`, `npm test`, `npm run test:integration`, `npm run export:web`, `npm run doctor`.

---

## 3. Estrutura

```
packages/mobile/
├── app/                          rotas (Expo Router)
│   ├── _layout.tsx               providers, boot da sessão, Stack.Protected (auth ⇄ app)
│   ├── +not-found.tsx
│   ├── (auth)/                   login · register · forgot-password
│   └── (app)/
│       ├── _layout.tsx           Stack do app autenticado
│       ├── _layout.tsx           ConversationDrawerProvider (conversas na lateral) + Stack
│       ├── (tabs)/               index (Início) · projects · files · agents · profile
│       ├── chat/[sessionId].tsx
│       ├── project/[projectId].tsx
│       ├── job/[jobId].tsx
│       └── usage.tsx · billing.tsx
├── src/
│   ├── config/env.ts             URL pública do Core
│   ├── services/api/             ÚNICO ponto de rede: client · errors · sse · upload · transport
│   │                             auth · sessions · projects · agents · usage · billing · voice · types
│   ├── services/session/         tokenStorage (.ts nativo / .web.ts) · jwt (exp só para UX)
│   ├── services/cache.ts         cache offline mínimo
│   ├── stores/                   auth (zustand) · pendingMessage
│   ├── features/                 chat (reducer + hook) · voice · files · agents
│   ├── components/               ui · chat · files · auth
│   ├── hooks/ · theme/ · utils/
│   └── __integration__/          testes contra o Core real
├── assets/brand/                 arte oficial copiada de packages/web/public/brand/master
├── app.json · eas.json · jest.config.js · jest.integration.config.js
```

---

## 4. Contratos do Core usados (auditados no código, sem invenção)

| Área | Endpoint(s) | Observações |
|---|---|---|
| Registro | `POST /auth/register` `{email,password,full_name}` → `{access_token,token_type,user_id,email}` | 409 e-mail existente; 422 senha < 8 ou > 72 bytes. |
| Login | `POST /auth/login` `{email,password}` → idem | 401 credencial inválida; 429 rate limit (IP e e-mail). |
| Perfil | `GET /auth/me` → `{id,email,full_name,plan,is_active,timezone,created_at}` | Chamado após login e no boot. |
| Fuso | `PATCH /users/me/timezone` `{timezone}` | Mesma regra do Web: o Core calcula o reset semanal. |
| Conversas | `GET/POST /sessions`, `GET/DELETE /sessions/{id}`, `DELETE /sessions/{id}/messages/from/{message_id}` | Datas sem fuso = UTC. |
| Mensagem (SSE) | `POST /sessions/{id}/message` `{content}` + `Idempotency-Key` | Endpoint unificado do Web; o Capability Router do Core escolhe chat ou agente. |
| Aprovação de ação | `POST /sessions/{id}/agent/approve` · `/reject` `{action_id}` | Gate de ferramentas CONFIRM. |
| Projetos | `GET/POST /projects`, `GET/PATCH/DELETE /projects/{id}`, `GET /projects/{id}/sessions` | DELETE arquiva. |
| Arquivos | `POST /projects/{id}/files` (multipart `file`, 10 MB), `DELETE /projects/{id}/files/{file_id}` | Não há armazenamento fora de projetos. |
| Uso | `GET /users/me/capacity` (percentuais, sem TCU), `GET /users/me/entitlement` | O Mobile não exibe TCU. |
| Billing | `GET /billing/plans`, `GET /billing/providers`, `POST /billing/checkout`, `GET /billing/orders[/{id}]` | Preço e provider decididos pelo Core. |
| Agentes | `GET /agents`, `GET /agent-instances`, `POST /agent-instances/{id}/pause|resume|run`, `GET /execution/jobs[/{id}]`, `POST /execution/jobs/{id}/cancel` | Runtime 100% no Core. |
| Voz | `POST /voice/transcribe` (multipart; `audio/mp4`, `audio/webm`…), `POST /voice/synthesize` | Provider e chave só no servidor. |

### Erros

FastAPI devolve `{"detail": ...}` em três formas, todas tratadas em `src/services/api/errors.ts`:
string, lista de validação (`[{msg}]`) e objeto (`{error, message, resets_at, resets_in}` nas cotas).

| HTTP | `ApiError.kind` | Comportamento no app |
|---|---|---|
| 401 | `unauthorized` | Em chamada autenticada: encerra a sessão local e volta ao login com aviso. No login: "E-mail ou senha incorretos." |
| 403 | `forbidden` | Recurso fora do plano (ex.: modo agente) — mensagem do Core + atalho para uso/planos. |
| 404 / 409 / 413 / 415 / 422 | `not_found` / `conflict` / `payload_too_large` / `unsupported_media` / `validation` | Mensagem do Core. |
| 429 com objeto | `quota_exceeded` | Limite do plano; mensagem do Core (inclui quando renova); sem "tentar novamente". |
| 429 com texto | `rate_limited` | Excesso de tentativas; pode tentar de novo depois. |
| 424 / 503 | `dependency_unavailable` | Serviço (ex.: voz) indisponível. |
| 5xx | `server` | Mensagem genérica — detalhes internos nunca são exibidos. |
| sem resposta | `network` / `timeout` / `aborted` | GET com até 2 novas tentativas (backoff); POST nunca repete sozinho. |

---

## 5. Sessão e segurança

- **Mecanismo real do Core:** JWT HS256 *stateless* no header `Authorization: Bearer`, validade de 24 h,
  **sem refresh token e sem endpoint de logout** (ver Bloqueadores).
- **Web do TRINDATRON** guarda o token em `localStorage` (`trinity_token`).
- **Mobile nativo:** `expo-secure-store` (Keychain/Keystore) com `WHEN_UNLOCKED_THIS_DEVICE_ONLY` — fora de
  backups e transferências de aparelho. Nunca AsyncStorage para o token.
- **Mobile Web:** não existe keychain no navegador e o Core não oferece sessão por cookie, então o token fica em
  `localStorage` — o mesmo *trade-off* do Web atual.
- **Boot:** token ausente → login; `exp` vencido (lido só para UX) → login com aviso; `/auth/me` 401 → login;
  Core inacessível → abre com o último perfil em cache e banner offline.
- **Sair:** apaga token e cache **neste aparelho**. O JWT continua válido no servidor até expirar.
- **Nenhum segredo no bundle:** o app só lê `EXPO_PUBLIC_API_URL`. Chaves de provider, banco e JWT secret
  ficam exclusivamente no Core.
- **Markdown:** nada é renderizado como HTML, imagens remotas não são carregadas e só links `http(s)`/`mailto` abrem.

---

## 6. Chat e streaming

Protocolo idêntico ao do Web (`packages/web/src/api/client.ts`), validado contra o Core real:

```
POST /sessions/{id}/message   Content-Type: application/json   Idempotency-Key: <uuid>
→ 200 text/event-stream
data: {"type":"status","key":"activity.chat.processing","message":"Processando"}
data: {"type":"text","content":"..."}                         (pipeline chat)
data: {"type":"tool_call"|"tool_result", ...}                 (pipeline agente)
data: {"type":"approval_required","action_id":...}
data: {"type":"done","session_id":...,"message_id":...,"user_message_id":...,"answer"?:...}
data: {"type":"error","message":...,"error"?:...}
data: [DONE]
```

- **Transporte:** `expo/fetch` expõe `ReadableStream` em todas as plataformas; `src/services/api/sse.ts`
  segue a especificação SSE (CRLF, `data:` multi-linha, comentários).
- **Rejeições antes do stream** (401, 403, 404, 429) viram `ApiError`; **falhas dentro do stream** viram
  `{kind:'error'}`; conexão encerrada sem `done` vira `interrupted`.
- **Renderização:** blocos de texto agrupados a cada 50 ms (≈20 renders/s em respostas longas).
- **Cancelar:** aborta a requisição. O Core não tem endpoint de cancelamento (ver Riscos).
- **Tentar novamente — detalhe importante descoberto na auditoria:** quando uma resposta falha, **o Core já
  gravou a mensagem do usuário**. Reenviar direto duplicaria a mensagem. O retry busca o histórico, apaga essa
  mensagem órfã com `DELETE /sessions/{id}/messages/from/{id}` e só então reenvia com uma nova `Idempotency-Key`.
- **Resposta de replay** (`Idempotency-Key` já concluída) chega como JSON; o app recarrega o histórico.
- **Voz:** botão de microfone grava (`audio/mp4` no nativo, `audio/webm` no Web), transcreve pelo Core e insere
  o texto no campo — o envio continua sendo uma mensagem normal.
- **Web:** Enter envia, Shift+Enter quebra linha (convenção do Web).

### Tela da conversa sobre `@kesha-antonov/react-native-chat` (2026-09-16)

A adoção foi feita com prova antes de migrar: `src/__tests__/chatLibrary.test.tsx` fixa os contratos de que a
migração depende. A lista é controlada (renderiza as mensagens recebidas e não guarda estado próprio), o envio passa
pelo nosso callback sem inserir nada por conta própria, e dá para injetar views nossas no lugar do conteúdo.

**O que a biblioteca faz** (`app/(app)/chat/[sessionId].tsx`)
- Estrutura: lista invertida, área abaixo dela e composer, dentro de um `KeyboardAvoidingView` que mede onde o
  chat começa na janela (`measureInWindow`). Continua sem offset fixo: nenhum número de altura de cabeçalho no código.
- Linhas memoizadas com comparação profunda dos dados. As partes vivas do turno (atividade e passos do agente) vão
  só na resposta em andamento (`src/features/chat/conversationRows.ts`), então um novo status re-renderiza essa
  linha, não a lista.
- Separadores de dia (`DaySeparator`, texto nosso): "Hoje", "Ontem" ou a data no idioma da pessoa, com o ano só
  quando não é o atual.

**O que continua nosso, sem troca por versão da biblioteca**
- Cada linha é desenhada pelo `MessageBubble`: Markdown, cards de mídia, passos do agente, estados
  interrompido/incompleto, copiar e ouvir (só quando o Core diz que a conta pode ouvir).
- Composer inteiro (anexos, voz, criações, capabilities). A biblioteca chama `renderInputToolbar` diretamente, sem
  remontar, então texto, anexos e gravação sobrevivem a cada re-render.
- Banners (aviso, aprovação, erro com "Tentar novamente" e "Ver uso e planos"), o reducer, o hook, o retry com
  reconciliação e a ausência de botão de parar.
- Botão "Ir para a mensagem mais recente". O da biblioteca não tem rótulo de acessibilidade, então fica o nosso,
  posicionado dentro da área da lista (acima dos banners), onde todas as plataformas entregam o toque.
- `maintainVisibleContentPosition` e `keyboardDismissMode` (interativo no iOS, ao arrastar no Android).

**Defeito da biblioteca contornado:** com a lista invertida, `renderChatEmpty` aparece de cabeça para baixo, porque
a FlatList só desvira o *style* do componente vazio, e um componente o ignora. Os estados vazio, carregando e erro
são desvirados por nós, e `chatScreen.test.tsx` verifica a orientação. Se a biblioteca corrigir o defeito, o teste
falha em vez de a tela mostrar o texto invertido.

**Jest:** `jest/reanimatedMock.js` e `jest/workletsMock.js` substituem o Reanimated e os worklets (o `mock` oficial do
Reanimated carrega o módulo real). O `jest.config.js` também aponta `react-native-reanimated/mock` para o nosso mock,
porque o `expo-router/testing-library` o re-registra. O botão de enviar da biblioteca é um `BaseButton` do
gesture-handler 3: em teste ele é pressionado pelo gesto (`fireGestureHandler`), não por `fireEvent.press`.

**Validação:** unitários, typecheck, lint e bundles exportados para Android (Hermes) e Web. Em runtime no Web,
contra um Core local descartável: abrir histórico, enviar com Enter, resposta com separador de dia, erro com banner
e retry sem duplicar a mensagem, rolar e voltar para a mais recente, e a folha de anexos. **Android em aparelho:
pendente** (ver §12).

---

## 7. Plataformas

| | Situação |
|---|---|
| **Android** | Configurado: `package com.trindatron.app`, ícone adaptativo com a arte oficial (margem de área segura, sem redesenho), edge-to-edge com `react-native-keyboard-controller`, botão voltar e deep links via Expo Router, permissões com textos em PT-BR, `RECORD_AUDIO` sem gravação em segundo plano. **Não executado em emulador/dispositivo nesta máquina** (sem Android SDK). |
| **iOS** | Configurado: `bundleIdentifier com.trindatron.app`, safe areas, `usesNonExemptEncryption: false`, textos de permissão de câmera, fotos e microfone. **Não executado** (exige macOS ou EAS Build). Compras de plano **desativadas no iOS** (App Store 3.1.1 exige In-App Purchase, que o Core não integra). |
| **iPad** | `supportsTablet: true`, orientação livre; a partir de 768 pt a barra de abas vira trilho lateral e o conteúdo é limitado a 760 pt de largura. Validado visualmente em viewports de iPad (retrato e paisagem) no build Web. |
| **Web** | `output: single` (SPA), URLs e deep links reais (`/chat/<id>`, `/project/<id>`), foco e teclado, Enter para enviar, confirmação via `window.confirm`. Build exportado e validado com Playwright contra o Core real. |

Deep links nativos: esquema `trindatron://` (ex.: `trindatron://chat/<id>`).

> Os identificadores `com.trindatron.app` derivam do domínio `trindatron.com` usado pelo Web. **Confirme-os antes
> do primeiro envio às lojas** — eles não podem ser trocados depois da publicação.

---

## 8. EAS (builds)

`eas.json` define:

| Perfil | Uso |
|---|---|
| `development` | Development build (`expo-dev-client`), distribuição interna, simulador iOS. |
| `development-device` | Igual, para iPhone físico. |
| `preview` | Build instalável para testes (APK no Android, ad hoc no iOS). |
| `production` | Loja; `autoIncrement` com versão remota. |

Passos (não executados — exigem a conta Expo da TRINDATRON):

```bash
npm i -g eas-cli
cd packages/mobile
eas login
eas init                                          # grava o projectId em app.json
eas env:create --environment preview --name EXPO_PUBLIC_API_URL --value https://<core-publico> --visibility plaintext
eas build --profile development --platform android
eas build --profile preview --platform all
```

Não publicar nas lojas antes de resolver os bloqueadores e riscos abaixo.

### Estado da validação em aparelho real (2026-09-13)

| Etapa | Android | iOS |
|---|---|---|
| A) build criado | **NOT DONE** — bloqueado por login EAS | **NOT DONE** — bloqueado por login EAS e conta Apple Developer |
| B) build instalado | NOT TESTED | NOT TESTED |
| C) fluxo testado em aparelho | NOT TESTED | NOT TESTED |
| D) fluxo aprovado | NOT TESTED | NOT TESTED |

Verificado sem conta (`npx expo config`, `npx expo prebuild --platform android`, `expo-doctor` 21/21):

- `com.trindatron.app` é o identificador em iOS e Android, sem divergência.
- **Bug corrigido:** `expo-image-picker` com `microphonePermission: false` gerava
  `RECORD_AUDIO tools:node="remove"` para o app inteiro, quebrando a gravação de voz no Android.
  O manifest agora declara `RECORD_AUDIO` (coberto por `src/__tests__/appConfig.test.ts`).
- **Corrigido:** `expo-audio` ativa reprodução em segundo plano por padrão, o que declarava
  `FOREGROUND_SERVICE_MEDIA_PLAYBACK` (exige justificativa no Google Play) sem uso pelo app. Desativado.
- `CAMERA` vem do manifest da própria biblioteca `expo-image-picker`.
- O projeto nativo iOS não pode ser gerado no Windows; só será verificado no EAS Build.

O que ainda impede um build legítimo:

1. **Login EAS** (conta Expo da TRINDATRON): `npx eas-cli@latest login`, depois `npx eas-cli@latest init` em `packages/mobile`.
2. **URL do Core alcançável pelo aparelho**: `localhost` não funciona em device. Opções: Core local na rede
   (`http://<IP-do-PC>:8000`, só para o development build com Metro) ou a API publicada. A escolha do
   servidor é do responsável pelo produto — o app nunca adivinha.
3. **iPhone real**: exige Apple Developer Program, registro do aparelho (`eas device:create`) e credenciais
   gerenciadas pelo EAS. O perfil `development` gera build de **simulador**; aparelho usa `development-device`.
4. **Android**: um aparelho físico com depuração/instalação de APK permitida (não há Android SDK nesta máquina).
5. Publicação (não para desenvolvimento): ícone 1024 px oficial.

---

## 9. Testes

| Comando | O que cobre |
|---|---|
| `npm test` | Unitários (backend mockado): client (auth, 401, retry, timeout, abort), erros, parser SSE, stream de mensagem (chat, agente, aprovação, erro, replay, cota, cancelamento), upload (progresso, 413, 401, cancelamento, rede, timeout), reducer e hook do chat (inclui a reconciliação do retry), store de sessão, Markdown seguro, navegação protegida e telas de Uso e Projetos. |
| `CORE_INTEGRATION_URL=http://127.0.0.1:8000 npm run test:integration` | **Core real, sem mocks:** registro/login/perfil/fuso/credencial errada, token inválido → 401, SSE de uma mensagem + reconciliação, projetos e arquivos, capacidade sem TCU, catálogo de billing, agentes/jobs e erros 404/422/voz. O teste pula sozinho sem a variável. |
| `npm run typecheck` · `npm run lint` | TypeScript estrito (`noUncheckedIndexedAccess`) e ESLint do Expo. |
| `npm run export:web` | Bundle Metro de produção. |

Para rodar a integração com um banco descartável (sem tocar no banco de desenvolvimento):

```bash
cd packages/core
DATABASE_URL=sqlite:///C:/tmp/mobile_it.db JWT_SECRET_KEY=<32+ caracteres> alembic upgrade head
DATABASE_URL=sqlite:///C:/tmp/mobile_it.db JWT_SECRET_KEY=<...> ALLOWED_ORIGINS='["http://localhost:8081"]' uvicorn src.main:app --port 8765
```

---

## 10. Bloqueadores do Core (`MOBILE_REQUIRES_CORE_API`)

Nenhum destes foi contornado inventando API no Mobile.

| # | Funcionalidade | Endpoint necessário (proposta) | Auth | Impacto hoje |
|---|---|---|---|---|
| 1 | Recuperar senha | `POST /auth/password/forgot {email}` → 202 · `POST /auth/password/reset {token,new_password}` → 204 | nenhuma | Tela explica e leva ao suporte (`contato@trinity.ai`, mesmo canal do Web). |
| 2 | Renovar e revogar sessão | `POST /auth/refresh {refresh_token}` → tokens · `POST /auth/logout` (revoga) | refresh / Bearer | Login a cada 24 h; "Sair" não invalida o JWT no servidor. |
| 3 | Anexos e imagem no chat | `POST /sessions/{id}/message {content, attachments:[file_id]}` e o Core usar o arquivo no contexto | Bearer | Câmera/galeria/documento só enviam para o armazenamento do projeto; **arquivos de projeto não entram no contexto do chat hoje** (só as instruções entram). |
| 4 | Visualizar/baixar arquivo | `GET /projects/{id}/files/{file_id}/content` | Bearer | Sem preview de arquivos já enviados (só antes do envio). |
| 5 | Notificações push | `POST /users/me/devices {token, platform}` · `DELETE /users/me/devices/{id}` + disparo em job concluído / aprovação pendente | Bearer | Nenhuma notificação; é preciso abrir o app. |
| 6 | Estado da assinatura | `GET /billing/subscriptions` (lista do usuário) | Bearer | Não é possível exibir nem cancelar assinatura no app (cancelamento via suporte). |
| 7 | Artefatos de execução | `GET /execution/jobs/{id}/artifacts/{artifact_id}` (URL assinada) | Bearer | Lista artefatos, sem download. Há trabalho não commitado no Core (armazenamento de artefatos) que pode cobrir isto. |
| 8 | Compra no iOS | Validação de recibo de In-App Purchase (ex.: `POST /billing/apple/verify`) | Bearer | Compra desativada no iOS. |

## 10.1 Experiência de produto (polimento comercial)

### Idioma
- `src/i18n`: mesma filosofia do Web — `pt`/`en`/`es`, chaves com ponto, fallback para `pt`, chave de
  armazenamento `trinity_locale`. As seções `activity.*` e `error.*` são **as mesmas chaves do Web**.
- Primeira abertura: idioma do sistema (`expo-localization`) — `pt-*`→Português, `en-*`→English,
  `es-*`→Español, outros→Português. Perfil → Idioma: **Automático**, Português, English, Español.
  Precedência: escolha manual > sistema (em Automático o app acompanha mudanças do sistema ao voltar ao app).
- Datas, horas, números e moedas usam `Intl` no idioma ativo. Textos de permissão do iOS localizados em
  `locales/{pt,en,es}.json`.
- `src/__tests__/i18nCoverage.test.ts` falha o build se faltar chave em algum idioma, se uma chave usada não
  existir ou se houver texto de interface fixo no código.

### Sessão
- Política: depois do login, o usuário só sai quando toca em **Sair** ou quando o Core prova que a sessão acabou.
- Bloquear a tela, ir para segundo plano, recarregar o bundle ou falhar uma leitura do Keychain/Keystore **não**
  desloga: o token usa `AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY` (legível com o aparelho bloqueado) e uma leitura
  indisponível é repetida quando o app volta a ficar ativo.
- A validade é decidida pelo Core, nunca pelo relógio do aparelho.
- **Web (corrigido em 2026-09-16):** `tokenStorage.web.ts` importava o formato da sessão de `./tokenStorage`, que no
  Web resolve para o próprio arquivo `.web.ts`. O import vinha vazio, a leitura sempre dava "indisponível" e o
  app parava em "Abrindo o TRINDATRON…". O formato agora fica em `storedSession.ts`, e `platformFiles.test.ts`
  impede que um arquivo de plataforma importe o próprio nome. O Jest resolve para o aparelho, por isso nenhum outro
  teste pegava o problema.
- Um 401 isolado não desloga: a sessão é confirmada em `GET /auth/me` (uma verificação por vez); só um 401 dessa
  confirmação encerra a sessão.
- **Causa da queda observada no aparelho:** durante a validação, o Core de teste foi reiniciado com outro
  `JWT_SECRET_KEY`, o que invalida todos os tokens emitidos antes. Isso é um 401 legítimo e irrecuperável sem
  refresh token.
- **MOBILE_REQUIRES_CORE_API — renovação e revogação:**
  - login/cadastro passam a devolver também `refresh_token` e `expires_in`;
  - `POST /auth/refresh {refresh_token}` → `{access_token, refresh_token, expires_in}` com rotação;
  - `POST /auth/logout {refresh_token}` → 204, revogando no servidor;
  - refresh inválido/revogado → 401. Com isso o Mobile renova em um único voo ao receber 401 e só desloga
    quando o refresh falhar. Sem esse contrato, a sessão dura o JWT de 24 h.

### Google
- `GoogleSignIn` (Entrar e Criar conta) usa `expo-auth-session`, trata cancelamento sem erro e troca o ID token
  do Google por uma sessão TRINDATRON **na mesma base de contas**.
- **Oculto hoje**, porque falta o contrato do Core e não há credenciais: nenhum endpoint ou client ID
  placeholder existe no app (`CORE_GOOGLE_SIGN_IN_PATH = null`).
- **MOBILE_REQUIRES_CORE_API:** `POST /auth/google {id_token}` → `TokenResponse`; o Core valida o token
  (assinatura, `aud` = client IDs do app, e-mail verificado) e vincula ao usuário existente pelo e-mail ou cria um.
- **Credenciais externas necessárias:** OAuth clients do Google Cloud para Android (SHA-1 da keystore do EAS),
  iOS (com o esquema de URL do client) e Web; depois `EXPO_PUBLIC_GOOGLE_ANDROID_CLIENT_ID`,
  `EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID`, `EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID` e o caminho do Core em `src/features/auth/google.ts`.

### Identidade: só TRINDATRON
- Nenhum tipo do app declara `model_default`, `model_used`, `provider_name` ou uso em tokens/custo: o que o Core
  enviar nesses campos não pode ser exibido.
- `src/services/api/redact.ts`: qualquer texto vindo do Core (erros, erro de stream, resultado e erro de agentes)
  que cite provider, modelo ou infraestrutura é trocado pela mensagem neutra do tipo de erro. Texto livre do Core
  sem tradução conhecida também não é exibido; o app mostra a mensagem localizada do tipo de erro.
- Passos de agente mostram só "ação executada" com rótulo traduzido, nunca a saída bruta da ferramenta.
- A resposta da IA sobre "qual modelo você usa" é regida pelo prompt de identidade do Core
  (`prompting/policy.py`), que o Mobile não altera. **Recomendado ao Core:** não enviar `model_used`/`model_default`
  e a mensagem crua de exceção (`str(exc)`) nas respostas de usuário.

### Cancelamento
- O composer não tem mais botão de parar/cancelar. Durante a resposta, o botão de envio vira um indicador
  "O TRINDATRON está respondendo". O abort interno continua existindo para limpar a conexão ao sair da conversa.

### Teclado
- A tela de conversa desenha o próprio cabeçalho. O `KeyboardAvoidingView` da biblioteca de chat
  (`react-native-keyboard-controller`, `translate-with-padding`) mede onde o chat começa na janela e reserva
  exatamente a altura do teclado a partir dali, **sem offsets fixos**. O composer remove o recuo do indicador
  inicial enquanto o teclado está aberto.
  Campo multilinha com altura máxima e rolagem interna; telas de login e cadastro usam `KeyboardAwareScrollView`.

### Uso do plano
- "Sessão atual" (percentual usado, restante e "Redefine às HH:MM" ou dia e hora) e "Esta semana"
  ("Reinicia <dia>, HH:MM"), com capacidade disponível e agentes persistentes quando o plano permite
  (`limits.max_agents ≠ 0`). Tudo de `GET /users/me/capacity` e `/users/me/entitlement`, sem TCU, no idioma e fuso do usuário.

### TEAM
- O Core ainda devolve o TEAM plano de US$ 199,90 em `/billing/plans`; o modelo por assento (Standard US$ 24,90 e
  Premium US$ 99,90, 2–150 assentos) está decidido em `docs/TEAM_SEATS.md` do Core, mas não implementado.
- O app **nunca exibe o TEAM antigo**. Um único ponto decide o que é vendável: `isCommerciallyAvailable` em
  `src/services/api/billing.ts`. Nenhum preço de TEAM existe no app.
- **MOBILE_REQUIRES_CORE_API:** o catálogo precisa trazer a unidade de cobrança por assento (por exemplo
  `billing_unit: "seat"`, `min_seats`, `max_seats` e o nível Standard/Premium) e o checkout precisa aceitar `seats`.

### Arquivos
- A aba Arquivos é a biblioteca do usuário em todos os projetos: busca, filtro por tipo (imagens/documentos) e por
  projeto, identificação do projeto de cada arquivo, abrir o projeto, enviar (câmera, galeria, documento) e apagar.
- **MOBILE_REQUIRES_CORE_API:**
  - `GET /files?query=&type=&project_id=&cursor=` (biblioteca paginada);
  - `POST /files` com projeto opcional;
  - `GET /files/{id}/content` (preview e download);
  - anexar em conversa: `POST /sessions/{id}/message {content, attachments:[file_id]}`, com o Core usando o
    arquivo no contexto.

  Preview, download e anexo não aparecem na interface até existirem no Core.

### Imagem, vídeo e avatar (2026-09-15)
- Criações são jobs do Core: `POST /media/images|videos` (idempotente), acompanhadas por `GET /media/jobs/{id}` e
  listadas em `GET /media/creations`. O Core escolhe a engine, faz fallback e cobra; o app não sabe qual engine rodou.
- Disponibilidade vem **só** de `GET /users/me/capabilities`. Sem essa resposta, criação fica desligada — o flag de
  plano `supports_image_generation` sozinho não põe botão na tela.
- Formato livre: um campo opcional aceita "1080x1920", "9:16", "Instagram Story", "banner"… e segue como `format`;
  sem ele, o próprio pedido carrega o formato. O Core ajusta ao mais próximo possível. Não existem mais as três
  opções fixas.
- Ações de toda criação pronta (`src/components/media/MediaActions.tsx`), iguais logo após criar, na conversa, em
  Criações e ao reabrir a conversa: **Abrir** (tela `creation/[jobId]` — imagem inteira ou vídeo com os controles do
  sistema), **Baixar** (Android: seletor de pasta do sistema, sem permissão de armazenamento; iOS: folha do sistema
  com "Salvar imagem/Salvar em Arquivos"; Web: download real), **Compartilhar** (folha nativa com o próprio arquivo;
  Web Share API com arquivo, ou download quando o navegador não compartilha arquivos) e **Criar outra**. O arquivo é
  lido por `download_path`/`content_path` com a sessão (`src/features/media/creationFile*.ts`).
- Módulos nativos: `expo-file-system`, `expo-sharing`, `expo-video` (sem reprodução em segundo plano nem PiP). O
  plugin do `expo-sharing` não é usado: ele serve para receber compartilhamentos, o que o app não faz. Exigem novo
  development build.
- Contrato e pesquisa de engines/licenças/custos: `docs/MEDIA_ENGINES.md`. Limites diários, tiers, resoluções e
  duração ficam só no Core.

### Navegação (2026-09-14)
- Abas: **Início · Projetos · Arquivos · Agentes · Perfil**. Conversas não são aba: ficam no espaço lateral
  (`ConversationDrawer`) — gaveta com botão de menu em toda aba e na conversa, gesto de borda na Home, voltar do
  Android fecha; a partir de 1024 pt o painel fica fixo ao lado do conteúdo. A Home mostra as recentes.
- Home: saudação → composer → uma dica → recentes → linha discreta do plano. Sem cards de painel.
- Dicas (`src/features/tips`): cada dica depende de uma capability real (ou, nas de upgrade, da falta dela no
  plano atual); rotação sem repetir a anterior; pt/en/es.
- Composer: `[ + ] [ texto ] [ microfone ] [ enviar ]` em Home, projeto e conversa. `+` abre câmera, galeria e
  documento (salvos no projeto da conversa ou num projeto escolhido — o Core ainda não anexa arquivos à mensagem) e,
  quando o Core oferecer, os atalhos de criação. Microfone com estado de gravação, descartar e transcrever.
- Idioma no Perfil: uma linha "Idioma · Automático · Português" que abre uma lista curta.
- Abertura animada e marca: `docs/BRAND_SYSTEM.md`.

### Developer Menu do Expo
- `expo-dev-launcher` e `expo-dev-menu` entram só como `debugImplementation`, com source set `disableInRelease`: builds
  `preview`/`production` não contêm menu, botão flutuante de ferramentas nem launcher.
- Só o perfil `development` usa `developmentClient` (coberto por `appConfig.test.ts`). Screenshots comerciais devem
  vir de `preview` ou `production`.

### Marca
- O recorte antigo da master tinha alpha duro (riscos e halos no escuro). Todos os arquivos agora saem de
  `scripts/brand/derive_brand_assets.py`: re-matte da master, só reduções, wordmark recortado do lockup oficial,
  camadas da abertura e ícones. A master fica intacta. Detalhes e regras: `docs/BRAND_SYSTEM.md`.
- `BrandSymbol` escolhe o menor arquivo oficial (64/128/256/512/573 px) com pixels suficientes para o tamanho e a
  densidade da tela. Coberto por `brand.test.ts`.

## 11. Alterações no Core

**Nenhuma.** `packages/core` não foi modificado. O Mobile se adaptou ao Core existente.

## 12. Riscos conhecidos

- **Nativo não executado aqui:** Android e iOS foram configurados e o bundle compila, mas não rodaram em
  emulador, simulador ou aparelho (máquina Windows sem Android SDK). Teclado, permissões, gravação de voz e
  safe areas precisam de validação em dispositivo via `eas build --profile development`.
- **Conversa sobre a biblioteca de chat, ainda sem aparelho:** o bundle Android compila e o Web foi validado em
  runtime, mas teclado (`translate-with-padding`), gestos do gesture-handler 3 e rolagem da lista invertida
  precisam ser conferidos em um Android real com o development build.
- **Cancelar geração** fecha a conexão; não há confirmação de que o Core interrompe a chamada ao provider
  nem de como contabiliza a resposta parcial.
- **Resposta de sucesso do chat** foi validada por contrato e testes, mas não com um provider real neste
  ambiente (o Core de teste rodou sem chaves de provider, por segurança e custo).
- Algumas mensagens do Core chegam em inglês (ex.: validação de senha).
- Token no `localStorage` na versão Web (mesmo modelo do Web atual) é exposto a XSS.
- Web em produção exige a origem do app em `ALLOWED_ORIGINS` do Core.

## 13. Pendências

- Criar/editar agentes no app (a API existe: `POST/PATCH /agents`).
- Ler resposta em voz alta (a API `POST /voice/synthesize` existe e já está no client).
- Memórias do usuário (`/users/me/memories`) e modos TRINITY CODE/WORK.
- `eas init`, variáveis por ambiente e primeiros builds `development`/`preview`.
- Ícone de 1024 px (a arte oficial disponível tem 512 px; não foi ampliada artificialmente).
