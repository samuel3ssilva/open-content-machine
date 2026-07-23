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

## Fontes privadas (biografia)

- ✅ Inventário metadata-safe com gate de aprovação (Sprint 1.2, Fase 1)
- ✅ Inventário real executado (outputs privados fora do repo; agregados apenas)
- ✅ Pacote de triagem (Fase 1.1): 19 textos candidatos aguardando revisão ·
  14 docs de projeto separados · 14 mídias deferidas (sem OCR) ·
  158 código/gerados excluídos
- ✅ Triagem do Founder concluída (15 aprovados / 4 rejeitados, validação limpa)
- ✅ Fase 2A: extração determinística local + 5 pacotes qualitativos sanitizados
  (redação de identificadores; originais intocados; tudo fora do repo)
- ✅ Founder autorizou análise qualitativa de 4 pacotes (STORY permanece bloqueado)

## Creator Intelligence

- ✅ Extração determinística
- ✅ Síntese qualitativa (4 pacotes; STORY nunca usado)
- ✅ Posicionamento (1 recomendação + 2 alternativas)
- ✅ Guia de voz preliminar (amostra limitada, rotulado)

## Content MVP

- ✅ Cinco ideias pontuadas · primeiro draft LinkedIn · adaptação X (fase anterior)
- ✅ Estratégia narrativa da série de 3 posts
- ✅ **Dois posts do Math Trail aprovados pelo Founder** (texto final canônico)
- ✅ Memória editorial privada atualizada (amostras canônicas, guia de estilo,
  decisões, histórico)
- ✅ **Post 1 publicado no LinkedIn (2026-07-23)** — coleta de métricas pendente
- 🔄 Post 2: preparação final de publicação (pacote de revisão pronto)
- ⬜ Post 3 (OCM): refinamento com a voz publicada

## Próximo gate

> Sprint 1 ✅ → run local real ✅ → hardening do classificador ✅ (Sprint 1.1:
> precisão high 100%, unknown sintético 9,6%) →
> inventário da pasta de biografia ✅ (Fase 1, somente metadados) →
> síntese qualitativa ✅ → drafts LinkedIn/X ✅ (privados, não aprovados) →
> **➡️ ESTAMOS AQUI: pós-publicação do post 1 — métricas + preparação do post 2** →
> inventário do archive LinkedIn → 2º run real (aprovado pelo Fable) →
> **v0.1.0**
>
> Nota: a revisão manual dos 100 títulos está pausada por decisão do Founder
> (arquivo preservado em ambiente privado); será substituída por uma amostra
> direcionada de 20–30 casos após o inventário do novo archive.

## Backlog principal (fora do sprint)

- ⬜ v0.2.0 — interpretação agregada com Claude/GPT (providers reais, TB-2)
- ⬜ Positioning & Creator Profile → Voice Vault → Oracle → Interview Panel
- ⬜ Drafting → Evidence Check → Council → Revision → Repurpose → Analytics
- ⬜ E-mail noreply nos commits (🔒 aguarda confirmação do Founder)
