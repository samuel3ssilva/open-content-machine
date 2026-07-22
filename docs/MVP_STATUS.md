# MVP Status — Painel Oficial

Atualizado: 2026-07-22 · Release atual: **v0.0.1** · Branch: `main` · CI: verde

Legenda: ✅ concluído · 🔄 em andamento · ⬜ não iniciado · 🔒 aguarda autorização do Founder

## Status geral

**Sprint 1 (Real-Data Audience MVP) em execução.** Fundação e pipeline
sintético entregues na v0.0.1. Dry-run, classificação, relatório expandido e
`export-public` implementados e testados em `main` (ainda sem release
marcada). Nenhum dado real foi processado.

## Fundação (v0.0.1)

- ✅ Repositório público, Apache-2.0, governança completa
- ✅ 6 agentes (Fable/Opus/Sonnet) e roteamento de modelos
- ✅ Arquitetura, ADRs 0001–0003, threat model, políticas de segurança/privacidade
- ✅ Pacote instalável + CLI offline (`demo`, `audience validate|anonymize|report`)
- ✅ Dataset sintético, schemas JSON, 84 testes, CI verde no GitHub Actions

## Audience Intelligence

- ✅ Pipeline sintético end-to-end (validate → normalize → anonymize → report)
- ✅ Anonimização determinística (HMAC + salt privado, allowlist, ADR 0003)
- ✅ `audience inspect --dry-run` (inspeção privacy-safe de arquivo externo)
- ✅ Variações de export (aliases PT/ES, ordem de colunas, falha clara)
- ✅ Classificação determinística por família de papel + senioridade + confiança
- ✅ Classificador em camadas com independência família × senioridade (Sprint 1.1)
- ✅ Relatório privado expandido (distribuições, segmentos candidatos, limitações)
- ✅ `audience export-public` (supressão de grupos < 10, rótulo "sanitized")
- ✅ Teste de performance com 8.000 registros sintéticos
- ✅ Dry-run executado contra o export real (somente metadados; 8.204 linhas, zero warnings)
- ✅ **REAL LOCAL RUN COMPLETED — AWAITING FOUNDER REVIEW** (outputs privados fora do repo; nada commitado)
- 🔒 Publicação da **v0.1.0** → após revisão do Founder

## Segurança e privacidade

- ✅ Zona privada git-ignored + endurecimento contra `*DataExport*`
- ✅ Incidente do export real: contido, forense limpa, relatório sanitizado
- ✅ 9 testes dedicados de privacidade + scan de PII/secrets no CI
- ✅ Runbook de dados reais com gate obrigatório de dry-run
- ✅ Parecer APPROVED FOR PUBLIC PUSH (bootstrap)
- ✅ Revisão Fable das fronteiras do dry-run e da supressão pública
- ✅ Parecer **APPROVED FOR REAL LOCAL RUN** emitido (2026-07-22)

## Próximo gate

> Sprint 1 ✅ → run local real ✅ → hardening do classificador ✅ (Sprint 1.1:
> precisão high 100%, unknown sintético 9,6%) →
> **➡️ ESTAMOS AQUI: inventário seguro (somente metadados) da pasta de
> biografia do Founder (Sprint 1.2, Fase 1)** → inventário do archive
> LinkedIn → 2º run real (aprovado pelo Fable) → **v0.1.0**
>
> Nota: a revisão manual dos 100 títulos está pausada por decisão do Founder
> (arquivo preservado em ambiente privado); será substituída por uma amostra
> direcionada de 20–30 casos após o inventário do novo archive.

## Backlog principal (fora do sprint)

- ⬜ v0.2.0 — interpretação agregada com Claude/GPT (providers reais, TB-2)
- ⬜ Positioning & Creator Profile → Voice Vault → Oracle → Interview Panel
- ⬜ Drafting → Evidence Check → Council → Revision → Repurpose → Analytics
- ⬜ E-mail noreply nos commits (🔒 aguarda confirmação do Founder)
