# DEV16 — referência da atualização

**Versão:** `0.12.0.dev16` · **Data:** 05/11/2026 · **Branch de integração:** `next`.
A implementação foi integrada pela [PR #19](https://github.com/TheViniAlmeida/session-migrate/pull/19).
É uma pré-release do fork; isso não representa publicação no PyPI.

## Compatibilidade

| Cliente | Contrato validado nesta entrega | Observações |
| --- | --- | --- |
| OpenCode | 2.2.5 | Destino de árvores, standalone ou servidor local conectado; import/export, API, títulos, `parentID` e retomada nativa testados. Contratos anteriores 2.0/2.1 preservados. |
| Claude Code | 2.1.317 e 2.1.318 | Contratos anteriores 2.1.209/303/306 preservados. Versões conhecidas misturadas são legíveis, mantendo cobertura UNKNOWN. |
| Codex | 0.166.0 | Leitura `.jsonl.zst`, resolução de `history_base`, IDs físicos de revert e forks/attached. Contratos anteriores permanecem disponíveis. |
| Python | 3.11 ou superior | 3.14 usa `compression.zstd`; 3.11–3.13 precisam de `libzstd` compatível no sistema para arquivos comprimidos. Ausência gera erro explícito. |

As versões exatas e os limites estão na [referência de compatibilidade](current-client-compatibility.md).
A atualização instala o migrador; não atualiza os executáveis dos clientes nem
migra conversas automaticamente. A instalação passou diretamente de DEV14 para
DEV16; DEV15 foi um candidato preparado, sem ativação.

## Funcionalidades

- Migração de uma sessão, subárvore ou árvore inteira, com novos IDs, títulos e
  publicação do pai antes das filhas. Cada destino recebe seu próprio histórico.
- Inventário com vínculos nativos comprovados ou declaração manual explícita;
  ciclos, órfãos, colisões e arquivos fora do root escolhido são recusados.
- Diário coordenador e ledger por nó, confirmação única, cancelamento observável
  e retomada de nós comprovadamente não iniciados. Importações incertas não são
  repetidas automaticamente.
- Capturas Codex limitadas e consistentes, separando hash físico, bytes
  materializados e autoridade composta. JSON ambíguo e expansão excessiva são recusados.

O Claude CLI não fornece um ponteiro pai confiável nos forks observados. Para
esses vínculos, inclusive quando os metadados privados do Desktop não estão
comprovados, use o [manifesto explícito](examples/session-tree-inventory-v1.json).
`parentID` preserva organização; não prova equivalência de estado privado ou
fronteira de fork. Atualização de uma sessão existente no mesmo ID continua fora
do contrato. Veja o [guia de árvores](tree-migration.md).

## Comandos novos

`smigrate` e `session-migrate` são aliases do mesmo programa.

| Comando | Função |
| --- | --- |
| `smigrate tree inventory` | Ler um inventário de uma origem explicitamente selecionada. |
| `smigrate tree review` | Preparar e mostrar a seleção, IDs, relações, perdas e limites. |
| `smigrate tree migrate` | Preparar novamente e publicar após confirmação atual. |
| `smigrate tree resume --operation-id UUID` | Revalidar uma operação e continuar somente nós comprovadamente não iniciados. |
| `smigrate tree inspect UUID` | Ler o diário e os resultados da operação. |
| `smigrate tree reconcile UUID` | Inspecionar a situação de reconciliação sem importar ou habilitar uma repetição automática. |

Opções principais: `--profiles`, `--source-profile`, `--source-client-version`,
`--manifest`, `--target-profile`, `--cwd`, `--model-provider`, `--model`,
`--mode single|subtree|full_tree`, `--anchor`, `--hierarchy-edge`, `--detach` e
`--acknowledge-partial`. `migrate` e `resume` pedem `yes` no terminal; `--yes`
fornece a confirmação explícita para uso não interativo. `--state-home` é global
e deve preceder `tree` quando usado.

```bash
smigrate --version
smigrate tree --help
smigrate tree migrate --help
smigrate tui
```

Na TUI, **Ctrl+T** abre a árvore visual. No dashboard, habilite as árvores com
**`--allow-tree --allow-publish`**, usando os perfis e projetos selecionados no
lançamento. Reinicie uma execução antiga da TUI/web para carregar a versão nova.
Os exemplos completos estão no [guia de migração](tree-migration.md).

## Endpoints HTTP do migrador

Estes caminhos pertencem ao dashboard do **SessionMigrate**, não à API nativa do
OpenCode. O servidor escuta em loopback. A API usa autenticação da execução atual;
as mutações também exigem `Origin` e `X-CSRF-Token`. A rota de árvores requer
escopo `publish` e lançamento com `--allow-tree --allow-publish`.

| Método e caminho | Situação | Função |
| --- | --- | --- |
| `POST /api/trees/prepare` | Novo | Criar job de preparação da árvore. Não publica sessões. |
| `POST /api/trees/{proposal_id}/confirm` | Novo | Confirmar a capability preparada nessa execução e iniciar a publicação. |
| `GET /api/profiles` | Reutilizado | Obter os IDs opacos de perfis e projetos autorizados. |
| `GET /api/capabilities` | Ampliado | Consultar `tree_available` e `publish_enabled`. |
| `GET /api/jobs/{job_id}` | Reutilizado | Acompanhar preparação/publicação e seus resultados. |
| `GET /api/proposals/{proposal_id}` | Reutilizado | Ler uma proposta disponível para revisão. |
| `POST /api/jobs/{job_id}/cancel` | Reutilizado | Solicitar cancelamento, preservando o resultado de publicação já iniciada. |

### Preparar

O corpo de `POST /api/trees/prepare` é um objeto JSON com:

| Campo obrigatório | Tipo/conteúdo |
| --- | --- |
| `source_profile_id`, `target_profile_id` | IDs opacos obtidos em `/api/profiles`; não são os nomes/IDs de configuração usados na CLI. |
| `project_id` | ID opaco de um projeto autorizado no lançamento. |
| `source_client_version` | Versão exata do contrato de origem. |
| `manifest` | Objeto do [inventário v1](schemas/session-tree-inventory-v1.schema.json), não caminho de arquivo nem proposta salva. |
| `mode` | `single`, `subtree` ou `full_tree`. |
| `detach` | Booleano: autoriza destacar a âncora de um pai omitido. |
| `acknowledge_partial` | Booleano: aceitação explícita das limitações/perdas apresentadas. |
| `model_provider`, `model` | Strings de configuração do destino. |

Campos opcionais: `anchor` (chave do nó) e `hierarchy_keys` (lista de chaves de
relações). O manifesto deve pertencer ao root/formato do perfil escolhido; HTTP
não escolhe outro HOME, root, executável ou arquivo arbitrário. O Web limita a
16 nós, 4 MiB por fonte e 8 MiB agregados.

A resposta identifica um job. Consulte-o até `reviewable` ou um estado de falha;
use o `proposal_id` e o `fingerprint` da proposta retornada para revisar e confirmar.
Um HTTP 200 significa que a solicitação foi atendida, não que toda a árvore já foi publicada.

### Confirmar e acompanhar

O corpo de `POST /api/trees/{proposal_id}/confirm` contém exatamente
`body_fingerprint` e `idempotency_key`. O primeiro deve corresponder ao
`fingerprint` revisado; o segundo identifica essa confirmação. Repetir a mesma
confirmação/chave retorna o mesmo job, sem duplicar a importação. Outra chave não
reutiliza uma capability consumida.

Acompanhe `GET /api/jobs/{job_id}` até `completed`, `failed`, `uncertain` ou
`cancelled`. Confira também o resultado por nó e sua cobertura. Não há endpoints
HTTP específicos `trees/resume`, `trees/inspect` ou `trees/reconcile` nesta versão;
use os comandos CLI correspondentes para o diário persistente.

Erros atuais: `403` para autenticação/origem/CSRF/escopo recusados; `400` para
entrada inválida; `413` para corpo acima do limite; `500` com
`operation_unavailable` para ação indisponível ou falha no backend. Não interprete
um erro após confirmação como autorização para iniciar outra importação.

## Validação e atualização instalada

A PR de implementação fechou **38 checks de CI aprovados**. Houve provas nativas
de quatro nós em standalone/servidor, reinício e continuação independente, além
de teste real do navegador com sandbox e controles de autenticação/replay.

A DEV16 foi instalada primeiro no operador e depois na compartilhada. Foram
conferidos 91 arquivos por prefixo e preservadas 15 dependências e o pip.
Sete contas passaram nas verificações. Cada instalação validou 533 casos únicos:
532 sucessos na rodada inicial e o mesmo timeout da TUI aprovado na repetição,
sem mudar código ou limite de espera. Os registros originais foram preservados.

Histórico detalhado: [changelog do fork](../changelog.md).
