# MVP Status — Painel Oficial

Atualizado: 2026-07-27 · Release atual: **v0.0.1** · Branch: `main` · CI: verde

## Public summary (English)

- `v0.0.1` is tagged and released; Sprint 1.x additions are merged on `main`
  but not yet in a tagged release.
- Current quality gates: 965 automated tests passing, `ruff` clean, `mypy`
  clean, CI green on GitHub Actions.
- The weekly AI & Claude Intelligence Brief (v0.1) is merged on `main` and
  runs **fully offline against synthetic fixtures only** — no connector, no
  scheduler, no real source, and no ranking calibration against real signals.
- One Founder-authorized real local run of a private connections export has
  been completed (metadata-only dry-run first, then one real run); outputs
  live outside this repository, nothing real was committed, and the shipped
  pipeline made no network calls (offline provider only).
- Editorial/content work (positioning, voice, drafts, published posts) is a
  human-led, AI-assisted workflow carried out **outside the shipped
  codebase** — see the clarifying note under Creator Intelligence below; no
  positioning/voice/oracle modules exist in `src/`.
- `v0.1.0` is pending the remaining release gates in
  [`release-gates-v0.1.0.md`](release-gates-v0.1.0.md).

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

## AI & Claude Intelligence Brief (semanal, v0.1 sintético)

> **Escopo:** tudo abaixo roda **offline, só com fixtures sintéticas**.
> Nenhum conector real (Gmail, RSS, HTML), nenhum scheduler ativo, nenhuma
> calibração contra sinal real. Todo brief termina em
> `awaiting_founder_review`; nada publica automaticamente.

- ✅ Schemas de inteligência + fixtures sintéticas (M1)
- ✅ Clustering e deduplicação determinísticos (M2)
- ✅ Ranking explicável (pesos 30/20/15/15/10/10, aritmética inteira,
  breakdown inspecionável) (M3)
- ✅ Níveis de evidência, tiers (Must-Understand / Should-Know / Radar) sobre
  um Top 10 sem backfill (M4)
- ✅ Intelligence Brief em Markdown + JSON (Tier 1 enxuto + apêndice completo) (M5)
- ✅ Biblioteca de tópicos persistente: lifecycle, histórico de score
  append-only, auditoria, regras de reconsideração (M6)
- ✅ Motor semanal `content-machine intelligence weekly-run` (M7): janela de
  7 dias com timezone, `run_id` determinístico, re-run idempotente,
  `--regenerate` explícito, escritas atômicas com rollback, 8 artefatos por
  semana
- ✅ **ADR 0009 — o skip idempotente verifica a própria premissa** (rodada de
  merge-gate, 2026-08-01): um `run_id` igual não prova mais, sozinho, que o
  run em disco é o que o código ATUAL produziria — `write_weekly_run_outputs`
  agora compara byte a byte cada arquivo que escreveria contra o que já está
  em disco antes de pular a escrita; se algo divergir (ex.: semântica de
  renderização/confiança mudou desde aquele run sem um bump de
  `code_version`), o run continua sendo pulado (nunca sobrescreve sem
  `--regenerate`), mas a CLI imprime um WARNING alto no stderr nomeando os
  arquivos divergentes — incondicional, mesmo sem `limitations-overlay.json`
  presente. **Procedimento operacional obrigatório:** aplicar uma correção a
  uma semana JÁ EXISTENTE exige `--regenerate` explícito, e o operador DEVE
  arquivar uma cópia do `output_dir` ANTES de rodar `--regenerate` — os
  arquivos `.bak.tmp` que `_atomic_write_all` usa internamente são apenas
  staging de rollback de UMA escrita atômica e são apagados automaticamente
  assim que ela tem sucesso; eles NÃO são um backup e não permitem recuperar
  o conteúdo pré-`--regenerate` depois do fato. Ver `docs/adr/
  0009-idempotent-skip-must-verify-its-premise.md` e o epílogo de
  `--help` do comando.
- ✅ Biblioteca v0.2: merge de tópicos com aliases, decay e staleness,
  relevância estruturada, resumo normalizado (≤280 chars, sem corpo bruto),
  deltas semanais de score/rank/tier
- ✅ ADR 0004 (D1–D8) + seção "Deferred to v0.3"
- ✅ Demonstração sintética de 3 semanas (outputs privados fora do repo)
- ⬜ Refinamentos v0.3 (decay consumido por output, fall-out reporting,
  `deltas.jsonl`, exibição do resumo normalizado)
- ✅ **Gate D — fundação de segurança de conectores (contratos + harness
  sintético)**: `content_machine.connectors` — permissões, retenção,
  sanitização, taxonomia de falhas, `run_discovery` com isolamento por fonte,
  e `bridge.to_source_item` (fronteira TB-4, fail-closed em provenance);
  ADR 0005; 7 adaptadores sintéticos determinísticos; **nada busca dados
  reais** — zero código de rede, zero credencial, zero scheduler, zero fonte
  real; zero linhas alteradas em ranking/rendering
- ✅ **Gate E0 — pré-requisitos de segurança dos conectores**: as quatro
  condições de entrada do ADR 0005 mais o mecanismo de configuração privada
  de endpoint. `security_flags` propagam até o brief (só nomes de flag, nunca
  texto hostil); gate vivo de permissão imediatamente antes da coleta, com
  `expires_at` fail-closed e relógio próprio que nenhum chamador pode
  fornecer; `may_supply_independence` consumido como conjunção que só pode
  REMOVER independência; `connectors/network.py` — o segundo e último módulo
  com I/O de rede — impondo HTTPS, allowlist de host **por fonte**, bloqueio
  de loopback/privados/link-local/IP literal, redirects revalidados a cada
  salto, defesa contra DNS rebinding fixando a conexão no IP vetado enquanto
  valida o certificado contra o hostname original, timeouts, corte de bytes
  no meio do stream, allowlist de MIME e rate limit; loader de config privada
  fail-closed com `SecretStr` e erros sanitizados; scan AST garantindo que só
  `config/` lê variáveis de ambiente. **Nenhum adaptador está ligado a esse
  fetcher** — é uma fronteira imposta e testada, ainda sem chamador. Zero
  chamadas externas, zero fonte real, zero credencial
- 🔒 Conectores reais (adaptador de verdade contra uma fonte real) → requer
  decisão de escopo do Founder + revisão de segurança/privacidade Fable antes
  de qualquer implementação

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

> **Nota:** os itens ✅ abaixo (Creator Intelligence e Content MVP) foram
> produzidos pelo fluxo editorial supervisionado do Founder, com apoio de IA
> em sessões fora do código versionado — **não** por módulos de software
> shipados. As fases correspondentes (Positioning & Creator Profile, Voice
> Vault, Oracle etc.) permanecem ⬜ não iniciadas como código em
> [`ROADMAP.md`](../ROADMAP.md).

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
> Eixo Intelligence Brief: M1–M7 ✅ merged em `main` (Gates A, B e C) →
> fundação de segurança de conectores ✅ (Gate D: contratos + harness
> sintético, ADR 0005 — nada busca dados reais) →
> **➡️ próximo gate de engenharia AGUARDA AUTORIZAÇÃO DO FOUNDER**, entre
> (a) refinamentos v0.3 dentro do escopo sintético e (b) o primeiro adaptador
> real sobre a fundação já construída. Conectores reais não começam antes de
> escopo/permissões definidos e revisão Fable.
>
> Nota: a revisão manual dos 100 títulos está pausada por decisão do Founder
> (arquivo preservado em ambiente privado); será substituída por uma amostra
> direcionada de 20–30 casos após o inventário do novo archive.

## Backlog principal (fora do sprint)

- ⬜ v0.2.0 — interpretação agregada com Claude/GPT (providers reais, TB-2)
- ⬜ Positioning & Creator Profile → Voice Vault → Oracle → Interview Panel
- ⬜ Drafting → Evidence Check → Council → Revision → Repurpose → Analytics
- ⬜ E-mail noreply nos commits (🔒 aguarda confirmação do Founder)
