# ADR-003: SQLite em desenvolvimento, PostgreSQL em produção

**Status:** Aceito  
**Data:** 2026-08-19

## Decisão
SQLite local com SQLAlchemy (portável). PostgreSQL via Supabase em produção.

## Alternativa rejeitada
PostgreSQL local (Docker bloqueado por BIOS; instalação nativa requer admin).

## Motivo
SQLAlchemy + Alembic abstraem completamente o banco.
Migração para PostgreSQL = mudar a connection string DATABASE_URL.
Zero código específico de banco em qualquer service.
