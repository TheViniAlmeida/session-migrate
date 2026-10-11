# Plano de implementação — evolução do SessionMigrate

Status: implementação em andamento. Spec/plano revisão 1, 09/10/2026; progresso atualizado em 10/10/2026.
F2A, F2B e F2C integradas em `next`; o hotfix de cancelamento pós-publicação
está concluído. F3 está integrada e instalada. F4A, a API privada do ledger, está integrada;
F4B está integrada com observação independente e intent durável. F4C está integrada
com assessment, recuperação explícita e adoção. A fase 4 foi publicada pela PR11
após revisão e validação. F5A implementa handoff literal, resumo revisado e retorno
para uma nova sessão; o gate de pacote/publicação está em execução. Full return
OpenCode permanece bloqueado; summary tem prova USER restrita com contexto
PARTIAL. F5B depende de prova de exclusão ou CAS do cliente nativo.
Os demais marcos seguem os gates abaixo.

Fonte normativa: [spec](../specs/interactive-migration.md), revisão 1.
Base de código: commit `662976e`. A fase 1 não integra este plano.
As checkboxes abaixo definem o plano; o todo registra execução e evidências por marco.

Progresso versionado:
`/storage/Agents/AgentZero/.ai-data/todos/2026-10/todo-session-migrate-evolution-101026.md`.
Antes de executar, adicionar os itens de cada marco ao todo, com responsável,
escopo e evidência. Checkboxes deste plano são um modelo de execução; o todo é
a fonte de progresso e deve apontar a versão aprovada destes documentos.

## 1. Decisões e ordem

```text
Spec aprovada
    → F2 núcleo compartilhado + perfis + TUI
    → F3 título + proposta detalhada
    → F4 ledger + checkpoints + avaliação de continuidade
    → F5A handoff/resumo/retorno → F5B update comprovado
    → F6 dashboard + uso/contexto
```

O spike de viabilidade F5B pode começar após a aprovação da spec, em paralelo
ao desenho F2/F3, sem habilitar writes de update no produto. Isso antecipa o
risco principal. Implementação F5B depende de F4; a fase 5 não se declara
completa apenas com F5A. Dashboard pode ter spike visual read-only após F4,
mas seus comandos de continuidade só são ativados conforme capacidades F5.

Arquitetura escolhida: core stdlib, Textual opcional para TUI, aplicação ASGI
local opcional para web, assets frontend empacotados. Antes de cada extra,
medir dependências/transitivas, Python suportado, licenças e custo de pacote;
fixar versões no lock e obter aprovação de dependência. Nenhuma dependência
dessas interfaces foi instalada para escrever este plano.

Ponto de atenção conhecido: `docs/specification.md` descreve o contrato antigo,
inclusive trechos desatualizados sobre versionamento OpenCode. Atualizá-lo
na entrega documental do primeiro marco aprovado, junto à arquitetura e
referência CLI, sem adicionar os novos comandos à PR #17 do adapter.

## 2. Contratos de integração

| Contrato | Conteúdo mínimo e dono |
| --- | --- |
| `SessionRef` | formato, perfil/root e native ID; resolução em `profiles.py`/catálogo |
| `Profile` | nome, raiz, CLI, HOME/XDG permitidos; sem segredo; `profiles.py` |
| `AdapterCapabilities` | schemas, título, mídia, formatos de source/target, update e proof version; `capabilities.py` |
| `TransferRequest` | source ref, target profile, CWD, schema/modelo, título e modo; `operations.py` |
| `TransferProposal` | snapshots, fingerprint das opções, ID reservado, artefato/perdas, effects e validade; `operations.py` |
| `TransferReceipt` | ID efetivo, target verificado, manifest, título/status e resultado; `operations.py` |
| `Checkpoint` | witnesses source/target, canonicalization/adapters, alinhamento e receipt; `lineage.py` |
| `ContinuityAssessment` | no-op, delta candidato, conflito, lineage broken ou reconcile; `continuity.py` |
| `UsageSample` | origem, chamada, provider/modelo, contadores, escopo, qualidade e provenance; `usage.py` |

CLI/TUI/web não implementam parsing nativo nem regras de perdas. A operação
compartilhada chama `load_session`, loaders virtuais, `convert_session`,
validadores e installers existentes. Novos adapters de título/update são
localizados por formato; não criar handlers HTTP que façam writes diretos.

Proposta de API core:
`resolve_source`, `read_preview`, `prepare_transfer`, `commit_transfer`,
`reconcile_transfer`, `assess_continuity`, `prepare_handoff`, `prepare_update`.
Nomes de continuidade são contrato proposto, não funções já disponíveis.
A implementação F3 usa `prepare_operation`, `proposal_metadata` e
`commit_operation`, além de `prepare_profile_operation` para perfis.
`PreparedOperation` é um handle privado em memória, consumido uma vez;
`TransferProposal` é metadado, não um comando executável persistido.
A CLI `smigrate proposal <convert|import|transfer>` descarta o handle após
emitir JSON schema1. Aplicar/reproduzir uma proposta entre processos depende
do ledger da fase4 e não é um comando F3.

Estados de operação: `preparing → ready → publishing → verifying → completed`;
terminais alternativos `cancelled`, `failed`, `needs_reconcile`. Um resultado
de import nativo seguido de falha local é `needs_reconcile`, não `failed` com
retry automático. A proposta é consumida uma única vez; mudanças de origem,
destino ou opções invalidam sua reutilização.

## 3. Fase 2 — núcleo, perfis e TUI

Requisitos: R01–R04 e R14. Primeiro marco utilizável.

### 2A. Extrair operação sem mudar a CLI

- [ ] Criar `operations.py`/contratos e extrair somente a orquestração de
  source selection, conversão, preflight, install e resultado de `cli.main`.
- [ ] Preservar exit codes, JSON atual, colisões, defaults e chamadas a CLIs
  nativas. `convert` permanece sem invocar target CLI.
- [ ] Passar environment por operação/subprocesso; manter proibida a mutação
  de `os.environ` para alternar perfis concorrentes.
- [ ] Representar progresso com callbacks/eventos content-free; nenhuma UI
  precisa interpretar mensagens de erro livres para descobrir sucesso.

Arquivos: `cli.py`, `conversion.py`, novo `operations.py`, testes existentes
de CLI/conversion/target integration e novos `tests/test_operations.py`.

Validação: testes de paridade dos comandos existentes, rotas e manifests;
colisão/source mutation; sucesso nativo seguido de falha de finalização;
isolamento de ambientes de duas operações. Gate completo do core antes da UI.

### 2B. Perfis e catálogo

- [ ] Criar `profiles.py` com perfil efêmero e armazenamento privado opcional.
  Reusar raízes do catálogo; importar perfil a partir de root conhecido exige
  confirmar formato e environment efetivo, especialmente OpenCode/Kilo.
- [ ] Reusar `Catalog.list_sessions`/`session_source_for_transfer` com paginação
  e filtros; identidade sempre inclui perfil/root. Ordenação/timestamps
  declaram sua fonte, sem inventar atividade por nome de arquivo.
- [ ] Revalidar um resultado do catálogo na leitura autoritativa. Perfil/ID
  duplicado, raiz ausente, CLI diferente e schema desconhecido têm erro claro.
- [ ] Adicionar capabilities centralizadas, com títulos/update inicialmente
  false/desconhecido até a prova do marco correspondente.

Arquivos: `catalog.py`, novos `profiles.py`/`capabilities.py`,
`tests/test_profiles.py` e testes do catálogo, sem indexar corpos.

Validação: duas raízes com mesmo native ID, títulos diferentes, arquivos
ausentes, arquivados/subagentes não suportados e dois stores OpenCode
isolados. Provar que a seleção de root usa o CLI/environment daquele store.

### 2C. Preview limitado e TUI

- [ ] Criar `preview.py` para projeção em páginas e cache limitado por snapshot;
  leitura/export/parsing executados em worker com timeout e orçamento.
- [ ] Adicionar extra `tui`, entrypoint `smigrate tui` via import lazy e pacote
  `tui/`: source/profile, lista/filtros, preview, target/options e resultado.
- [ ] Navegação por teclado, estados de loading/error/empty, resize, retorno
  entre etapas e cancelamento pré-publicação. Subprocessos/IO ficam fora do
  loop; cancelamento após `publishing` mostra estado, sem matar importador.
- [ ] Renderizar texto literal e remover controles perigosos somente da
  projeção visual; histórico transferido mantém semântica validada do adapter.
- [ ] Mostrar resumo básico e confirmação antes de instalar; instrução de
  resume aparece como ação do usuário, nunca autoexecução.

Arquivos: `preview.py`, `tui/{app,screens,widgets}.py`, assets TCSS,
`pyproject.toml`, `uv.lock`, `tests/test_preview.py`, `tests/test_tui.py`.

Validação: Pilot/headless, TTY ausente, extra ausente, cancelamento, resize,
Unicode/markup/OSC, worker lento sem UI travada, cache invalidado e só sessão
selecionada carregada. Fixture de catálogo com 10.000 metadados para medir
paginação/busca; benchmark registrado, não gate flaky de wall-clock em CI.

### Gate da fase 2

Usuário conclui por teclado Claude→Codex, Codex→OpenCode 2.0 e
OpenCode 2.0→Claude, com perfis exatos, preview e nova sessão retomável.
Oracle nativo usa clients exatos/fixtures sintéticas; a CI desse gate rejeita
skips. Demais formatos continuam na matriz sem regressão. Wheel core funciona
sem extras; wheel com `tui` abre a interface. Nenhum store real nos testes.

## 4. Fase 3 — nome e proposta detalhada

Requisitos: R05–R06 e R14. Depende da operação e dos perfis F2.

### 3A. Título e capacidades nativas

- [ ] Adicionar `ConversionOptions.target_title`, `--target-title` e exigência
  opcional de título nativo, preservando `--title`/`--session-id` atuais.
- [ ] Propagar override sem alterar `Session` da origem. Revisar caminhos de
  install que hoje usam `artifact.source.title`, para usar o título efetivo
  do target sem contaminar campos de proveniência.
- [ ] Spikes de título nativo Claude/OpenCode/Codex: mapear contrato e picker,
  validar Unicode/controles/limites e cold resume. Codex não ganha capability
  true só por escrever um campo que o picker ignora.
- [ ] Implementar títulos comprovados; demais adapters retornam alias local
  ou unsupported explícitos. Alias vive no app state, não substitui nome nativo.

Arquivos: `conversion.py`, `operations.py`, `capabilities.py`, adapters
selecionados e testes sintéticos/nativos de título por harness.

### 3B. Proposta, prévia e confirmação

- [ ] Proposta preparada contém source snapshot e target preflight, ID, título,
  schema, counts, loss counters e effects. Invalidar ao editar qualquer campo.
- [ ] Mostrar perdas/avisos na TUI e nos novos resultados JSON; manter contrato
  de saída legado ou fazer adições versionadas e documentadas.
- [ ] Validar de novo no commit. Rejeitar proposta antiga, ID em colisão e
  raiz/environment/CWD diferente dos apresentados na confirmação.
- [ ] Expor título solicitado/efetivo/status, perdas, ID, manifest e resume.
  Não apresentar alias como alteração do picker nativo.

Validação: origem append entre preview/confirm, colisão tardia, mudança de
modelo/schema/perfil/título, dry-run OpenCode com effects explícitos, alias
marcado e exigência nativa recusada. Nenhum arquivo de source modificado.

### Gate da fase 3

Uma operação concluída prova ou declara a persistência do nome, e a prévia
coincide com a operação/manifest efetivos. CLI automatizada continua compatível.
Cobertura nativa prioritária e fixtures de limites do OMP/nomes multibyte.

## 5. Fase 4 — ledger e continuidade

Requisitos: R07–R08 e R14. Depende de propostas/receipts F3.

### 4A. Estado durável e recuperação

- [ ] Criar `lineage.py`, SQLite próprio para operações/arestas/checkpoints,
  separado do catálogo. Versionar schema; migration exige backup e aprovação.
- [ ] Definir receipt verificável, estados e ID de operação; persistir antes
  de publicar, confirmar target e fechar ledger após sucesso.
- [ ] Recuperar interrupções em cada fronteira. Se native import ocorreu,
  verificar ID/artefato e reconciliar; nunca resolver retry com nova importação.
- [ ] Lock por target/operação para concorrência entre instâncias do migrator;
  declarar que isso não bloqueia agentes externos.

Arquivos: `lineage.py`, `operations.py`, `jsonl.py` apenas se necessário,
`tests/test_lineage.py`, `tests/test_operation_recovery.py`.

### 4B. Canonicalização e witnesses

- [ ] Implementar serialização canônica própria de eventos portáteis, incluindo
  payloads de ferramenta/mídia. Não usar equality do dataclass.
- [ ] Versionar algoritmo e projection policy. Guardar hashes/witnesses e
  alinhamento de source/target realmente confirmados após instalação.
- [ ] Testar split/group, IDs/timestamps sintetizados e perdas conhecidas.
  Alinhamento ambíguo falha fechado; não usar título/UUID como checkpoint.
- [ ] Manter manifestos v2 legíveis. Novo envelope v3 ou receipt separado tem
  leitor versionado; importações antigas são untracked até vínculo explícito.

### 4C. Avaliação de continuidade

- [ ] Criar `continuity.py`: compare snapshots com witnesses, identifique
  no-op/delta/conflito/lineage broken/reconcile, preservando fronteira de turno.
- [ ] Validar ligação call/result e impedir corte no meio de ferramenta.
  Compactações e mudança de branch só permitem delta com prova correspondente.
- [ ] TUI apresenta alternativas para cada estado; não fazer merge automático.

Validação: A→B→A, várias idas/voltas, mensagens repetidas, mesmo UUID em homes
diferentes, payload diferente com texto igual, argumentos alterados,
target/source ambos avançados, truncate, compaction e falha pós-import.
Apagar/reconstruir catálogo não pode apagar linhagem; testes de corrupção e
schema antigo/novo rejeitam estado inseguro.

### Gate da fase 4

Repetição comprovada é no-op. Delta candidato não inclui conteúdo importado
anteriormente. Cada lado tem checkpoint próprio, diferenças de projeção não
viram perda silenciosa, e restauração de processo interrompido é determinística.
Ledger permanece privado e sem corpos de mensagem.

## 6. Fase 5 — handoff, resumo, retorno e update

Requisitos: R09–R11 e R14. Depende de F4 para retorno/delta/update.

### 5A. Handoff e retorno em nova sessão

- [ ] Criar `handoff.py`: representação estruturada com fatos/trechos, fontes,
  limites e campos editáveis. Renderizar texto/Markdown/JSON sem inventar estado.
- [ ] Extra/provedor de resumo é opcional: configurar referências de credencial,
  modelo, budget e limites; não copiar stores de auth do harness. Gate humano
  para dependência/settings/env e confirmação de envio no produto.
- [ ] Preparar resumo com conteúdo selecionado/segmentado e omissões; mostrar
  saída editável, fonte e orçamento antes de aceitar. Timeout/falha retorna
  ao handoff offline; nunca gasto/retry automático sem limite.
- [ ] Importar resumo como contexto identificado fornecido pelo usuário,
  sem fabricar turnos antigos/resultados de tools ou promover system prompts.
- [ ] `return-new` resolve perfil anterior a partir da linhagem, permite revisar
  destino e cria sessão nova, com recibo/aresta nova e perdas declaradas.

Validação: deterministic handoff com faltas explícitas, texto hostil como dado,
resumo por provider falso local (nada pago em CI), orçamento/timeout/cancelamento,
nenhuma credencial/log de corpo, full e summary com retomada nativa.

### 5B. Spike e update in-place

- [ ] Investigar target Claude Code primeiro, depois variantes suportadas de
  Codex: comandos/APIs nativas, schema, índices e controle de concorrência.
  Registrar proof por client/schema; não generalizar para Codex paginado ou
  OpenCode 2.0 pelo importer de novas identidades.
- [ ] Demonstrar como reservar a mesma identidade contra um harness externo.
  Simular cliente append durante proposta/publicação. Sem exclusão/CAS
  comprovados, não habilitar update; decidir bloqueio com o operador.
- [ ] Para estratégia viável, adicionar adapter de update: source estável,
  target checkpoint válido, delta completo, backup imediato e verificado,
  plano de recuperação por fase, publicação nativa/atômica e verificação.
- [ ] Confirmar ID/prefixo e bytes/estado protegidos; testar cold resume e um
  novo turno persistido naquele mesmo ID. Preservar dados anteriores além do
  subconjunto portátil: update não é rewrite destrutivo de todo o target.
- [ ] Habilitar CLI/TUI `update` somente para proofs passados. Outros targets
  recebem motivo e opções handoff/return-new. Toda mutação de store existente
  exige alvo, backup e confirmação explícitos.

Arquivos: `continuity.py`, `operations.py`, `capabilities.py`, novo
`formats/update_*.py` apenas para formatos comprovados, testes de crash/races,
CI native-update selecionada e docs de recuperação por adapter.

### Gates da fase 5

5A: usuário produz handoff revisado/resumo opt-in e volta a uma sessão nova
vinculada, retomável, sem duplicação no assessment.

5B: Codex→Claude Code atualiza o ID original quando elegível; repetir é no-op;
atividade, divergência, compaction ambígua e parcialidade bloqueiam writes;
falhas deixam resultado identificável/recuperável. Prova nativa sem skip.
Se a estratégia não for segura, 5B fica bloqueada: fallback não é conclusão.

## 7. Fase 6 — dashboard, API e uso/contexto

Requisitos: R12–R13 e R14. Depende do core/ledger F2–F4 e capacidades F5.

### 6A. Métricas e proveniência

- [ ] Criar `usage.py` separado do modelo de replay: `UsageSample`, escopo
  cumulativo/delta, origem native/imported/estimated/unknown e cobertura.
- [ ] Extrair uso confiável primeiro de Claude, Codex e OpenCode. Adapters
  podem precisar leitura complementar controlada de usage, sem guardar corpos.
- [ ] Associar uso posterior à linhagem/checkpoint e IDs nativos de chamadas.
  Zeros sintetizados/importações antigas sem proveniência não viram consumo real.
- [ ] Implementar estimativa portátil rotulada; tokenizer/dependência opcional
  só com gate. Não estimar percentual com janela desconhecida nem omitir framing
  nativo como se fosse contexto exato. Preço/custo só com fonte/modelo/data.

Validação: cumulativo versus delta, billing duplicado por cópia, cache/read/write,
zeros sintetizados, usage parcialmente ausente, modelos diferentes, compactação
e diferença entre estimativa e request nativo observado pelo provider falso.

### 6B. API local e jobs

- [ ] Criar extra `web`, `web/app.py`/routes/contracts/jobs e comando explícito
  `smigrate web`, loopback, sem daemon/global process/service/firewall.
- [ ] Rotas propostas: perfis/capabilities; sessions e preview paginado;
  proposals; confirmação por proposal ID; jobs/results; lineage; usage;
  handoff/update somente se a capability permitir. A API não aceita raw path.
- [ ] Auth efêmera por execução, Host/Origin/CSRF, sem CORS aberto; bootstrap
  browser com arquivo efêmero privado selecionado pelo usuário, sem token
  em URL/argv/log/storage; testar owner/mode/expiração e nenhuma rota para
  baixar o arquivo. Errors/progresso não vazam corpos.
- [ ] Enfileirar jobs pelo mesmo core, serializar mesmo target, limitar
  threads/processos/export subprocesses e bounds de requests/páginas.
- [ ] Rotas mutáveis possuem idempotency key ligada a proposta/operação;
  restart reconcilia ledger, sem republicação cega. Cancel respeita publishing.

Validação: client ASGI/fake workers, auth/token/Origin/Host, DNS rebinding,
paths fora de perfil, requests oversized, XSS/markup, duas confirmações,
concorrência ao mesmo target, cancel e retomada de jobs interrompidos.

### 6C. Interface web e empacotamento

- [ ] Criar `dashboard/` com assets locais e build separado do site institucional.
  Telas e componentes seguem os fluxos da spec; estados disabled têm motivos.
- [ ] Catálogo/preview paginados; filtros/perfis, nome, proposta/perdas,
  confirmação, progresso, resume/result, lineage e métricas por origem/qualidade.
- [ ] Mídia não faz network automático; CSP/DOM literal e acessibilidade básica
  por teclado. Conteúdo de conversa não entra em analytics/session replay.
- [ ] Build frontend gera assets para o pacote `web/static/`. Wheel inclui
  arquivos de produção; runtime não exige Node, internet nem CDN.
- [ ] Teste navegador cobre selecionar sessão→confirmar→resultado, conflito,
  logout/token expirado, preview hostil, métricas incompletas e dados importados.

### Gate da fase 6

Mesmo `TransferRequest` tem as mesmas perdas/resultado em CLI/TUI/web; o web
só oferece ações suportadas. Token/auth invalidam acesso inclusive local;
nenhum outro usuário ganha acesso pelo processo. Core wheel sem extras passa;
web wheel serve assets e conclui um fluxo sintético offline. Metrics indicam
fontes e lacunas; não apresentam estimativa como uso real ou consumo duplicado.

## 8. Divisão de trabalho e revisão

| Área/owner por contrato | Paralelismo permitido |
| --- | --- |
| Core/CLI/propostas | Um owner; contratos revisados antes de integrar UI |
| Perfis/capabilities/catálogo | Paralelo ao esqueleto TUI, depois de `SessionRef` estável |
| Preview/TUI | Usa stubs tipados do core; não edita adapters/installers |
| Título/native proofs | Owner por adapter, após contratos F3 definidos |
| Ledger/checkpoints | Um owner de schema; assessment separado após canonicalização |
| Update/native proofs | Após spike e contratos F4; sem concorrência com mudanças de adapter iguais |
| Usage | Separado de `Session`; paralelo ao web se contrato de sample estável |
| API/jobs | Core estável; owner de auth e publicação |
| Frontend | API contract versionado; fixtures locais, sem depender de sessão real |
| Revisão | Independente, diff integrado e gates reais, não só relatório de executor |

Cada delegação nomeia arquivos, todo, proibidos, modelo/esforço e aceite;
um escritor por arquivo/schema/todo. Gates de dependência, provider, banco e
update são fechados antes de executor/headless. Trabalho paralelo não autoriza
instalar dependência, alterar políticas ou expandir target silenciosamente.

## 9. Gates gerais e releases

Em cada marco: Ruff/lint/format, pytest afetado, `git diff --check` e revisão.
Mudança de core/adapters: suíte/matriz de regressão e oracle exato requerido,
wheel/sdist e entrypoints instalados. Interfaces: testes de interação/jobs e
instalação core/tui/web separadas. CI core Python3.11–3.13; versão adicional
somente após verificar compatibilidade das dependências escolhidas.

Nativos selecionados não passam por skip; gates históricos ausentes ficam
NÃO VALIDADO, sem somar contagens de rodadas diferentes. Nenhuma validação usa
sessões reais, account credentials, fornecedor pago ou stores de produção.

Cada marco tem branch/PR própria no fork, escopo revisável e release com versão
distinta/proveniência do fork. Não misturar UX/ledger/web à PR #17 OpenCode.
Proposta de linha de desenvolvimento: criar `next` no fork a partir de
`662976e`; documentação e melhorias abrem PRs para `next`, mantendo a branch
da PR #17 intacta. Criar/publicar essa linha requer autorização específica;
esta proposta não altera branches agora.
Exemplos de divisões: core-perfis; TUI; título/proposta; ledger; handoff; update
por adapter; métricas; API; frontend. A integração ocorre por dependências dos
contratos, não por uma PR monolítica.

Releases propostas: primeira entrega F2/F3; segunda F4/F5A; terceira F5B após
prova; quarta F6. Numeração definitiva e publicação são decisões do operador.
Se F5B bloquear, F6 pode expor F5A e sinalizar update indisponível sem declarar
todas as fases concluídas. Proposta de capacidade e marcos fica visível no todo.

## 10. Saída desta tarefa de planejamento

Pronto quando: spec escrita antes do plano, todos R01–R14 mapeados a tarefas e
aceites, revisão independente concluída, links conferidos e riscos nativos
explicitados. Fase 1 ausente das tarefas deste plano. Nenhum comando novo,
framework, ledger ou update implementado apenas por aprovar os documentos.

Publicação documental autorizada pelo operador em 09/10/2026: linha `next`,
branch `docs/interactive-migration-spec-plan` e PR para o próprio fork.
Próxima etapa de implementação, mediante pedido do operador: F2A e spikes de
título/update, com gates próprios de dependências/migrations/operações sensíveis.
