# ADR-001: Python + FastAPI para TRINITY CORE

**Status:** Aceito  
**Data:** 2026-08-19

## Decisão
Backend principal em Python 3.12 + FastAPI.

## Alternativa rejeitada
Node.js + Express/Fastify.

## Motivo
Ecossistema de IA em Python (LiteLLM, Anthropic SDK, langchain) é incomparavelmente superior.
FastAPI oferece streaming nativo, OpenAPI auto-gerado, e suporte async de primeira classe.
Node.js continua sendo usado no Frontend (Desktop + Web).
