# Spec — migração interativa, continuidade e dashboard

Status: revisado e autorizado para publicação. Revisão 1, 09/10/2026.
Progresso em 10/10/2026: F2 integrada, hotfix de publicação concluído;
F3 integrada e instalada; F4A integrada como API privada do ledger.
F4B está integrada; F4C segue em implementação e revisão.
Esta spec continua descrevendo também fases futuras.

Base: fork no commit `662976e`, com OpenCode 2.0 e contrato legado preservado.
Escopo: fases 2–6 do pedido. A instalação da versão corrigida é uma entrega
operacional separada e não faz parte desta spec nem do plano abaixo.

Plano derivado: [implementação](../plans/interactive-migration-implementation.md).
Progresso: `/storage/Agents/AgentZero/.ai-data/todos/2026-10/todo-session-migrate-evolution-101026.md`.

## 1. Objetivo e alcance

Permitir escolher uma sessão por harness e local, examinar seu conteúdo,
selecionar destino e nome e transferir com previsibilidade. Registrar a relação
entre conversas para permitir voltar ao harness anterior, produzir handoff e,
quando o contrato nativo permitir, atualizar a sessão existente. Oferecer essas
operações também em um dashboard web local opcional, com uso e contexto.

O núcleo continua Python 3.11+, CLI programável e adapters dos 18 harnesses.
Claude Code, Codex e OpenCode 2.0 são as primeiras rotas de aceitação nativa.
Os demais aparecem no seletor com suas capacidades e limitações efetivas.
Séries de schema não verificadas continuam bloqueadas pelo adapter.

Não entram nesta proposta: sincronização contínua automática, edição arbitrária
do histórico, execução de ferramentas antigas, transferência de credenciais,
restauração de processos/subagentes, servidor multiusuário, acesso remoto via
SSH, SaaS ou leitura administrativa das sessões de outros usuários.

## 2. Situação atual e mudança de contrato

| Evidência no código | Consequência para a evolução |
| --- | --- |
| `cli.py` concentra seleção, conversão e instalação | Extrair uma operação compartilhada para CLI, TUI e web |
| `catalog.py` já busca por títulos/IDs e múltiplas raízes | Reutilizar a descoberta, sem criar índice de corpos |
| `Session`/`Event` representam história portátil | Usar o mesmo modelo no preview e na continuidade |
| `ConversionOptions` não possui título de destino | Introduzir `target_title`; manter `--title` como seletor de origem |
| `codex.serialize` contabiliza perda de título | Nome nativo no Codex precisa de spike e oracle próprios |
| Importadores atuais recusam colisões | Atualização deve ser uma operação nova, com contrato próprio |
| OpenCode 2.0 import aceita identidade nova, não overwrite | Importar de novo com o mesmo ID não implementa update |
| Uso não está representado no modelo portátil e writers sintetizam zeros | Separar uso registrado, histórico importado e estimativas |
| `website/` é o site institucional | Dashboard terá aplicação local própria, usando o núcleo Python |

O contrato atual de transferência para uma nova sessão permanece. Os novos
comandos não mudam o significado dos comandos existentes. Alterações futuras
na spec geral deverão declarar explicitamente a nova operação `update`.

## 3. Requisitos identificados

| ID | Requisito | Fase |
| --- | --- | --- |
| R01 | Selecionar harness e perfil/local de origem | 2 |
| R02 | Buscar, filtrar, paginar e visualizar uma sessão | 2 |
| R03 | Selecionar harness, perfil/local e projeto de destino | 2 |
| R04 | Transferir pela mesma operação usada na CLI | 2 |
| R05 | Definir e verificar título de destino, distinguindo alias local | 3 |
| R06 | Exibir prévia, perdas, efeitos e resultado da transferência | 3 |
| R07 | Registrar linhagem, checkpoints e resultado durável | 4 |
| R08 | Detectar conteúdo novo, repetição, compactação e divergência | 4 |
| R09 | Gerar handoff e transferência resumida | 5 |
| R10 | Voltar ao harness anterior com nova sessão vinculada | 5 |
| R11 | Atualizar a mesma sessão quando o adapter provar suporte | 5 |
| R12 | Oferecer dashboard local opcional com as mesmas operações | 6 |
| R13 | Mostrar tokens, contexto e histórico de operações com proveniência | 6 |
| R14 | Preservar compatibilidade, privacidade e isolamento por usuário | Todas |

## 4. Fluxo da TUI — fase 2

Comando explícito: `smigrate tui`. Executar a CLI sem argumentos mantém o
comportamento anterior. Sem TTY ou extra `tui`, retornar orientação curta e
erro apropriado, sem instalar nada automaticamente.

Fluxo principal:

1. Escolher harness e perfil de origem: home/raiz, CLI nativa quando necessária
   e projeto opcional. Exibir o perfil ativo antes de listar sessões.
2. Listar por páginas, buscar título/ID e filtrar por projeto, data,
   arquivamento e disponibilidade. Corpos não entram na busca inicial.
3. Abrir preview sob demanda: mensagens em ordem, resumo de estrutura,
   ferramentas recolhidas por padrão, compactações e limitações conhecidas.
4. Escolher harness e perfil de destino, CWD/projeto, modelo/provider quando
   exigidos e modo de transferência disponível.
5. Preparar a operação, revisar e confirmar. A revisão básica já existe na
   fase 2; a fase 3 acrescenta título e detalhamento de perdas.
6. Executar, mostrar progresso, ID/título efetivos, manifesto e instrução de
   retomada correspondente ao destino. Retomada exige ação do usuário.

Escolher o mesmo harness é permitido para migração entre locais. IDs iguais
em raízes diferentes nunca são usados sem a raiz/perfil exatos. Entradas
corruptas, ausentes, ocupadas ou não transferíveis permanecem visíveis com
motivo; não podem ser confirmadas como uma migração válida.

A interface deve operar por teclado, aceitar mouse como complemento, manter
seleção ao buscar, voltar etapas sem perder campos e suportar resize/SSH.
Esc encerra leitura/edição ou cancela trabalho antes da publicação. Uma
publicação nativa iniciada não é interrompida para fingir rollback.

### Perfis e preview

Um perfil contém nome, formato, raiz, caminho da CLI e overrides permitidos
de HOME/XDG para aquele subprocesso. CWD é um campo separado. Perfis não
armazenam segredos, não alteram o ambiente global nem copiam config da origem
para o destino. Em OpenCode/Kilo, a raiz escolhida deve corresponder ao store
efetivo usado pelo CLI exportador/importador; não basta filtrar o catálogo.

Catálogo é metadado derivado. O preview relê a origem autoritativa e verifica
sua identidade. Leitura/exportação e parsing ficam fora do loop da UI,
limitados pelo adapter. Renderizar páginas, não toda a conversa de uma vez.
Cache de preview é limitado, em memória e invalidado quando a origem muda.
Conteúdo não vai para logs, SQLite de catálogo ou telemetria.

Texto da sessão é dado: não interpretar markup, HTML, escape ANSI/OSC ou
comandos. Arquivos e imagens remotos não são buscados automaticamente.
Inicialmente, mídia aparece como metadado; visualização de mídia exige uma
ação explícita e continua respeitando os limites de tamanho/formato.

## 5. Destino, nome e prévia — fase 3

Adicionar `--target-title` aos comandos aplicáveis. `--title` continua sendo
busca da origem; `--session-id` continua sendo identidade nova do destino.
Ausência de override preserva o comportamento atual do adapter.

Capacidades de título: `native_verified`, `local_alias` ou `unsupported`.
O resultado informa título solicitado, título efetivo e tipo da persistência.
Alias do SessionMigrate aparece como alias, sem alegar mudança no picker do
harness. O usuário pode exigir título nativo; um destino sem suporte recusa
essa exigência antes de instalar. Título do source nunca é editado.

Prioridade de prova: Claude Code, OpenCode 2.0 e Codex. Cada implementação
de título nativo precisa de reabertura pelo harness/picker ou API nativa
equivalente. Comportamento textual do arquivo sozinho não é prova. Valores
vazios, controles, Unicode e limites específicos, como o slot do OMP, têm
tratamento definido e erro explícito; não truncar silenciosamente.

Uma proposta imutável registra origem/fingerprint, perfil e CWD de destino,
schema/modelo, identidade reservada, título, modo, contagens e perdas.
Editar qualquer opção invalida a proposta. Confirmar relê/preflighta o estado;
origem modificada ou destino em conflito exige preparar novamente.

No marco F3, `smigrate proposal <convert|import|transfer>` emite metadados
JSON schema1 sem publicar sessão, manifest ou alias e sem pedir confirmação
no stdin. Probes/exportações nativas podem atualizar seu estado de runtime;
esses efeitos devem ser declarados. O handle preparado pertence ao processo,
é consumido uma vez e não pode ser aplicado a partir do JSON emitido.
Persistência/recovery de propostas pertence à fase4.

`--local-title-alias` exige escolha explícita; `--require-native-title`
recusa alias e operações convert-only que não verificam persistência nativa.
O limite do app é 1024 bytes UTF-8, sem normalizar ou truncar Unicode válido.
Provas de título nativo são específicas para cada versão exata do cliente;
OpenCode 2.0.22/2.0.23 não habilitam versões futuras automaticamente.


A prévia mostra efeito concreto: nova sessão ou update, local e ID de destino,
conteúdo/compactações que serão preservados, classes omitidas, avisos de versão
e efeitos de probes nativos. Dry run não instala a sessão/manifesto; OpenCode
pode inicializar seu próprio cache/store no probe, como no contrato atual.

## 6. Linhagem e checkpoints — fase 4

Guardar identidade com formato + perfil/root + native ID. UUID isolado não é
identidade suficiente. Uma operação relaciona source, target, snapshots,
versão dos adapters e transformação aplicada. Cada volta cria uma aresta nova;
linhagem é grafo, não um par que vai sendo sobrescrito.

Ledger durável, separado do catálogo descartável: operação, estados, receipts,
arestas e checkpoints. Nenhum corpo de conversa ou credencial é gravado ali.
Manifests continuam content-free; novos campos versionados não alteram
retrospectivamente manifestos v2. Uma falha ao gravar o ledger após import
retorna `needs_reconcile`, com target ID e prova disponível, sem auto-reimport.

Checkpoint contém duas observações: prefixo source selecionado e projeção
target confirmada após import/export/reabertura. São possíveis diferenças de
agrupamento, IDs sintetizados e perdas. Não comparar source e target como se
fossem cópias byte a byte.

Canonicalização inclui kind, role, texto, argumentos/resultados de ferramentas,
ligação lógica call/result e hashes de mídia. Exclui IDs/timestamps sintetizados
e runtime privado conforme contrato de projeção. Não usar `Event == Event`:
`payload` tem `compare=False` e perderia diferenças nos argumentos.

Guardar witnesses suficientes para detectar extensão do prefixo e mudanças,
incluindo a versão da canonicalização e alinhamento conservador entre lados.
Ambiguidade em mensagens repetidas, mudança de branch, truncamento ou nova
compactação sem correspondência comprovada bloqueiam o delta automático.

| Estado observado desde o checkpoint | Ação disponível |
| --- | --- |
| Source estendeu prefixo; target manteve projeção confirmada | Delta candidato |
| Nenhum lado mudou e receipt existe | No-op: não duplicar mensagens |
| Ambos avançaram | Conflito: handoff ou nova sessão com escolhas explícitas |
| Prefixo mudou/compactou sem alinhamento confiável | `lineage_broken`; transferência completa/handoff |
| Operação interrompida após instalar | Reconciliação por prova do target; não importar cegamente |

Identidade/fingerprint/checkpoint, não apenas hash do arquivo, determinam
idempotência. Migrações antigas sem checkpoint são `untracked`: podem ser
vinculadas somente após comparação autoritativa e escolha explícita; não criar
um checkpoint presumido a partir de nomes semelhantes.

## 7. Handoff, resumo, retorno e update — fase 5

Modos distintos:

- `full`: reescrever história portátil para uma identidade nova.
- `return-new`: voltar ao perfil/harness anterior numa nova sessão vinculada.
- `handoff`: documento portátil com objetivo, decisões, estado, verificações,
  pendências e próximo passo, acompanhados de fontes/limites.
- `summary`: história reduzida revisada, importada como sessão nova.
- `update`: acrescentar delta a um target existente com capacidade comprovada.

Handoff inicial é offline: extrair limites/trechos e permitir edição do
usuário; ausência de dado vira desconhecido, não uma decisão inventada. Resumo
por modelo é opcional e separado. Antes de enviar, escolher provider/modelo,
conteúdo, orçamento e confirmar o envio; credencial é obtida por referência
no ambiente do usuário, nunca copiada de autenticação de outro harness.
Sem confirmação, funciona o handoff offline. Sem truncamento silencioso para
encaixar em orçamento; seleção/segmentação e omissões ficam explícitas.

O texto resumido é identificado como handoff fornecido pelo usuário, não
como mensagens históricas completas, resultado de ferramenta nem instrução
privilegiada. Não promover prompts existentes para system/developer.

### Atualização da mesma sessão

`update` não usa import com ID existente nem força overwrite. Precisa de um
checkpoint válido, target selecionado, delta revisado, backup imediato e
estratégia específica do adapter. A origem desta operação permanece intacta;
um antigo source pode agora ser o target escolhido na volta.

Primeiro spike: retorno Codex→Claude Code. Depois: target Codex nos schemas
efetivamente suportados. OpenCode 2.0 não ganha update pelo importer atual.
Destinos/versões sem prova recebem `update_supported=false` e oferecem
`return-new`/handoff, sem alegar que atualizaram o ID anterior.

Uma estratégia in-place deve provar coordenação exclusiva ou transação/CAS
nativa, preservação do ID/prefixo/estado requerido, publicação recuperável e
retomada/append pelo harness. Lock do migrator sozinho não prova exclusão
de um CLI externo. Se não houver controle confiável de concorrência, a
estratégia permanece indisponível; não reduzir essa exigência para liberar UI.

A fase 5 tem duas entregas separadas: 5A handoff/resumo/return-new; 5B update
in-place. **5B não está concluída com apenas o fallback.** Para concluí-la,
ao menos o retorno ao Claude Code deve passar a prova nativa; se o spike
não viabilizar um contrato seguro, registrar bloqueio e a decisão do operador.

Proibir update em conversa ativa, estado divergente, ferramenta parcial sem
fronteira segura, checkpoint inválido ou schema não verificado. Sem merge
automático de dois ramos. Repetir a mesma atualização comprovada é no-op.
Falha após escrita fica recuperável/`needs_reconcile`; nunca sucesso presumido.

## 8. Dashboard e métricas — fase 6

`smigrate web` é um processo local opt-in, com extra `web`. Serve apenas o
usuário que o inicia, em loopback, sem daemon/startup automático. Não abrir
firewall nem anunciar acesso externo. A aplicação não reutiliza o site
institucional como backend de sessões.

Telas: catálogo/filtros; detalhe e preview; formulário de transferência;
proposta/confirmação; progresso/resultado; grafo de continuidade; operações e
uso/contexto. As capacidades desabilitadas exibem motivo, igual à TUI.
Mutações sempre passam pelo mesmo núcleo, proposta e validação nativa.

API usa IDs opacos de perfis/sessões/operações; uma URL não aceita path de
arquivo arbitrário. Conteúdo somente sob autenticação local por token efêmero
por execução. Token não vai para URL, argumento do browser, log ou storage
persistente. Bootstrap inicial usa arquivo de acesso efêmero por execução,
privado (`0700` no diretório, `0600` no arquivo), selecionado pelo usuário no
browser; somente o caminho pode ser exibido pela CLI. O browser lê o token
localmente e o mantém em memória. O arquivo expira ao encerrar o processo,
não é credencial de harness e não é servido por uma rota HTTP. O formato
runtime do arquivo e sua limpeza passam o spike de auth antes do web MVP.
Validar Host/Origin, proteção CSRF das mutações, sem CORS amplo. CSP restrita,
texto literal, sem scripts/conteúdo remoto da sessão. Erros/logs omitirem corpos
e credenciais. Autorização/local scopes também valem em localhost.

Jobs têm progresso e estado persistido. Antes da publicação podem cancelar;
depois dela apenas acompanhar/reconciliar. Transferências concorrentes ao
mesmo target/checkpoint são serializadas; falhas são exibidas de forma útil.

### Métricas com proveniência

| Dado | Como apresentar |
| --- | --- |
| Contador nativo por chamada/mensagem, schema conhecido | Uso registrado, origem e coverage explícitos |
| Histórico importado de outro harness | Separado do consumo posterior naquele destino |
| Zero sintetizado por writer | Não converter em medição real; mostrar desconhecido/importado |
| Contador cumulativo | Diferenciar de delta; não somar com mensagens individuais |
| Tokens de conteúdo portátil | Estimativa, tokenizer/modelo/método/versionamento explícitos |
| Janela de contexto do modelo | Somente metadado confiável/valor configurado; senão desconhecida |
| Custo | Somente custo nativo ou preço/modelo/data conhecidos; nunca total inventado |

Rastrear importação e chamadas novas evita contar a mesma história migrada
como consumo novo em cada harness. Importações antigas sem proveniência são
`origin_unknown`. Compactações, system/tool schemas e framing nativo fazem
estimativa portátil diferir da requisição real. Percentual de janela só aparece
quando ambos numerador e limite têm fonte identificada e qualidade declarada.
Não prometer contexto efetivo do próximo request sem oracle/prova do cliente.

## 9. Arquitetura e pacote propostos

Núcleo shared: perfis/resolução → leitura autoritativa → proposta → publicação
→ verificação → receipt/linhagem. CLI, TUI e web são consumidores desse núcleo.
Adapters continuam responsáveis pelo schema nativo; UI não escreve stores.

Módulos propostos: `profiles.py`, `capabilities.py`, `operations.py`,
`preview.py`, `lineage.py`, `continuity.py`, `handoff.py`, `usage.py`,
`tui/`, `web/`. Preferir funções pequenas e extrair só o necessário de
`cli.py`; não reescrever todos os adapters para criar um framework genérico.

Contratos internos: `SessionRef`, `Profile`, `AdapterCapabilities`,
`TransferRequest`, `TransferProposal`, `TransferReceipt`, `Checkpoint`,
`ContinuityAssessment`, `UsageSample`. São objetos tipados, sem import de UI
no core. JSON de comandos atuais preserva seu contrato; novos formatos têm
schema versionado e exemplos sintéticos.

Proposta de dependências: Textual no extra `tui`; Starlette/Uvicorn no extra
`web`; estimador específico somente em extra futuro se necessário. Core segue
stdlib, com imports opcionais lazy. Versões/lock serão escolhidos e aprovados
no início dos respectivos marcos, sem instalar novos frameworks nesta tarefa.
Frontend inicial: assets HTML/CSS/TypeScript locais compilados e incluídos no
wheel, sem CDN e sem build Node exigido do usuário final.

Catálogo continua descartável; ledger/perfis duráveis vivem no state/config
privados do usuário. Diretórios novos `0700`, arquivos `0600`, respeitando
permissões existentes. Não usar instalação compartilhada como store global.
Backup obrigatório antes de migrations do ledger e de updates nativos.

## 10. Aceite, riscos e entrega

| Marco | Critério obrigatório |
| --- | --- |
| Fase 2 | Seleção de raízes ambíguas, preview limitado e transferência via teclado nas rotas prioritárias, sem bloquear UI |
| Fase 3 | Título nativo ou alias identificado; mudanças invalidam proposta; perdas/simulação/resultado coerentes com manifesto |
| Fase 4 | Checkpoints de ambos os lados; no-op repetido; divergência/compactação ambígua bloqueadas; recuperação após import parcial |
| Fase 5A | Handoff offline e resumo opt-in com revisão; retorno em nova sessão retomável e vinculado |
| Fase 5B | Update no mesmo ID comprovado, incluindo repetição, concorrência, falha e retomada nativa; fallback não conta como conclusão |
| Fase 6 | Mesma semântica do core/TUI, API local protegida, jobs recuperáveis e métricas sem dupla contagem/certeza falsa |

Testes usam fixtures sintéticas e HOME/XDG isolados. Gates de cliente exato
não podem passar por skip. Default suite informa o que não foi validado.
UI tem testes headless de navegação, estado/cancelamento e preview hostil;
API tem testes de autorização, origin/host/path scoping e concorrência.
Matriz de conversão/regressões e CLI existente permanecem gates do core.

Entregas pequenas por marco, branch/PR e revisão independentes. A spec e o
plano precisam de aprovação de escopo antes da implementação. Adicionar
dependências, migrations, configuração de provider/porta e update de store
existente exigem os gates próprios; este documento não concede tais ações.

Riscos centrais: títulos sem API nativa; stores/versões privadas; exclusão de
concorrência no update; alinhamento após compactação; lacunas de uso; custo e
privacidade de resumo externo. Spikes resolvem esses pontos antes de ativar
capacidades na interface. Nenhuma capacidade é habilitada por suposição.

## Referências

- [Contrato atual](../specification.md), [arquitetura](../architecture.md),
  [catálogo](../session-catalog.md), [OpenCode v2](../opencode-v2.md).
- [Textual: workers](https://textual.textualize.io/guide/workers/) e
  [testes headless](https://textual.textualize.io/guide/testing/).
- [Extras de pacote Python](https://packaging.python.org/en/latest/specifications/pyproject-toml/).
- [Starlette middleware](https://www.starlette.io/middleware/) e
  [Uvicorn settings](https://www.uvicorn.org/settings/) — referências para o
  spike web, não aprovação de configuração padrão ou instalação.
