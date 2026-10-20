# TRINDATRON

Plataforma de IA agentic com chat por streaming SSE, workspaces de código e tarefas autônomas.

**Stack:** FastAPI · SQLAlchemy · Alembic · SQLite/PostgreSQL · Next.js 14 · Electron · TypeScript

---

## Pré-requisitos

| Ferramenta | Versão mínima | Verificar |
|---|---|---|
| Python | 3.12+ | `python --version` |
| uv | qualquer | `uv --version` |
| Node.js | 20+ | `node --version` |

> **Windows:** os comandos abaixo funcionam em PowerShell. Bash via Git Bash também é suportado.

---

## Início rápido — Backend (TRINITY CORE)

```powershell
cd packages\core

# 1. Instalar dependências Python
uv sync

# 2. Copiar e preencher variáveis de ambiente
copy .env.example .env
# Edite .env e preencha ANTHROPIC_API_KEY (obrigatório para chat funcionar)

# 3. Aplicar migrations — OBRIGATÓRIO na primeira execução e após atualizações
uv run alembic upgrade head

# 4. Iniciar o servidor
uv run uvicorn src.main:app --reload --port 8000
# API disponível em: http://localhost:8000
# Health check:      http://localhost:8000/health
```

> **Nota:** se o backend iniciar sem `alembic upgrade head`, todas as rotas que acessam
> o banco retornam 500. Sempre rode as migrations antes do primeiro startup em um ambiente limpo.

---

## Início rápido — Frontend Web (TRINITY WEB)

```powershell
cd packages\web

# 1. Instalar dependências Node
npm install

# 2. O arquivo .env.local já aponta para http://localhost:8000 (backend local)
# Para apontar para outro backend: edite packages/web/.env.local
#   NEXT_PUBLIC_API_URL=http://localhost:8000

# 3. Iniciar em desenvolvimento
npm run dev
# Disponível em: http://localhost:3000
```

---

## Variáveis de ambiente obrigatórias

### Backend — `packages/core/.env`

| Variável | Obrigatória | Descrição |
|---|---|---|
| `ANTHROPIC_API_KEY` | Sim | Chat e agent falham sem ela |
| `JWT_SECRET_KEY` | Em produção | Mínimo 32 chars, não pode ser o placeholder |
| `DATABASE_URL` | Em produção | PostgreSQL obrigatório em prod (`sqlite://` aceito em dev) |
| `ENVIRONMENT` | Em produção | `production` força validações de segurança no startup |
| `ALLOWED_ORIGINS` | Em produção | JSON array de domínios; sem localhost |

### Endpoints de saúde

| Endpoint | Acesso | Conteúdo |
|---|---|---|
| `/health` | público | liveness; usado pelo healthcheck do Railway |
| `/health/db` | público | readiness; testa conexão com o banco |
| `/health/metrics` | **admin** | agregados de tráfego, planos, provider/modelo, custo/TCU e demanda por capability |

`/health/metrics` exige um usuário com `is_admin=True` (mesmo JWT, mesmo gate
de `src/api/deps.py`). Os valores são agregados — nunca conteúdo de mensagem,
id de sessão/usuário ou credencial — mas descrevem a economia do produto e por
isso não ficam públicos.

### Frontend — `packages/web/.env.local`

| Variável | Padrão | Descrição |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | URL do backend sem trailing slash |

---

## Migrations

```powershell
cd packages\core

# Aplicar todas as migrations pendentes
uv run alembic upgrade head

# Ver revision atual do banco
uv run alembic current

# Ver histórico de migrations
uv run alembic history
```

O banco usa **SQLite em desenvolvimento** e exige **PostgreSQL em produção** (SQLite não suporta
escrita concorrente). O backend recusa iniciar em `ENVIRONMENT=production` com SQLite.

---

## Comandos de desenvolvimento

### Backend

```powershell
cd packages\core

# Servidor com auto-reload
uv run uvicorn src.main:app --reload --port 8000

# Testes
uv run pytest tests/ -v

# Script auxiliar (PowerShell)
.\..\..\scripts\dev-core.ps1
.\..\..\scripts\test-core.ps1
```

### Model Router (Adaptive Intelligence Router)

```powershell
cd packages\model-router

uv run pytest -q     # suite completa
```

Arquitetura, invariantes de failover, Quality Recovery e a regra de segurança
para recusas legitimas: `docs/ADAPTIVE_INTELLIGENCE_ROUTER.md`.

> **Atencao — dependencia de caminho.** `packages/core` instala
> `packages/model-router` como copia (nao editavel). Depois de editar o
> router, `uv sync` sozinho **nao** atualiza o venv do Core, porque a versao
> nao mudou. Rode:
>
> ```powershell
> cd packages\core
> uv sync --dev --reinstall-package trinity-model-router
> ```
>
> CI e o build Docker de producao partem de um checkout limpo e nao sao
> afetados.

### Frontend Web

```powershell
cd packages\web

npm run dev          # desenvolvimento (http://localhost:3000)
npm run typecheck    # verificação TypeScript (tsc --noEmit)
npm run test         # testes unitários (vitest)
npm run build        # build de produção (next build)
npm run start        # serve o build de produção
```

### Desktop (Electron)

```powershell
cd packages\desktop

npm install
npm run dev          # abre janela Electron em modo dev
npm run build        # build de produção
```

---

## Produção — checklist mínimo

O backend **recusa iniciar** se algum item abaixo estiver errado com `ENVIRONMENT=production`:

- [ ] `DATABASE_URL` aponta para PostgreSQL
- [ ] `JWT_SECRET_KEY` com 32+ caracteres, não é o placeholder
- [ ] `ANTHROPIC_API_KEY` preenchida
- [ ] `DEBUG=false`
- [ ] `ALLOWED_ORIGINS` sem localhost

Gerar um JWT secret seguro:
```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Aviso Windows — litellm cleanup

Ao encerrar testes, pode aparecer no console:

```
ValueError: I/O operation on closed file.
```

É um erro cosmético do cleanup assíncrono do litellm no Windows. O exit code dos testes
sempre será 0. Não afeta funcionalidade nem CI.
