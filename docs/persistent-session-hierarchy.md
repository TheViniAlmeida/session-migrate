# Árvores de sessões persistentes

Status em 05/11/2026: **runtime implementado na DEV16**, com CLI, TUI e dashboard.
O [guia de uso](tree-migration.md) descreve a entrada compacta, os limites e a retomada.
O schema de proposta abaixo permanece referência de projeto; a saída de runtime
é uma visão de revisão, nunca um documento que reconstitui autorização.
O primeiro destino proposto é o OpenCode **2.2.5**, a partir de sessões Claude
selecionadas explicitamente. O runtime cobre sessões independentes que
continuam disponíveis após reiniciar o cliente, inclusive filhas e netas.
Subagentes efêmeros ficam fora desse escopo.

O [esquema JSON Schema 2020-12](schemas/session-tree-v1.schema.json) descreve uma
proposta de migração e um contrato de eventos do diário. O
[exemplo de três níveis](examples/session-tree-v1.json) é sintético, contém
declarações manuais e está deliberadamente bloqueado. Seus hashes não são
testemunhos de arquivos reais. Nenhum desses arquivos é um comando executável,
uma autorização de publicação ou uma demonstração de suporte ao Claude Desktop.

## Evidência disponível e limites

| Origem/destino | O que foi observado | Implicação |
| --- | --- | --- |
| Claude Code CLI 2.1.317 e 2.1.318 | `--fork-session` cria outro ID, copia o histórico e permite continuar em processo separado; o arquivo do pai permanece byte a byte igual. As capturas não registraram `parentSessionId`, `spawnedFrom` nem `forkedFromSessionId`; `isSidechain` é falso. | O fork persistente existe. A relação entre arquivos não pode ser reconstruída automaticamente dessas capturas. `parentUuid` relaciona mensagens, não sessões. |
| Claude Desktop, aba Code | Há relato de forks aninhados e comentários sobre `local_<id>.json`, `spawnedFrom`, `forkedFromSessionId` e `lineageDetached`. | É uma pista de investigação. Sem fixture própria e versão/interface identificadas, não existe contrato de decodificação aprovado. |
| Codex 0.166.0 | `forked_from_id` indica a origem do fork; `attached_thread` distingue o fork anexado. `parent_thread_id` corresponde ao vínculo de subagente. O teste de fork comum encontrou `session_id` igual ao novo ID, sem agrupar todos os forks pela origem. | Origem de fork, agrupamento visual e vínculo de subagente são fatos diferentes. `session_id` sozinho não identifica uma árvore. |
| Codex 0.166.0, histórico paginado | `history_base` referencia prefixo físico; o limite lógico do fork tem campo próprio, `forked_from_ordinal_exclusive`. | O prefixo pode mudar sem mudar o pai lógico. A DEV16 resolve esse prefixo dentro do root autorizado, validando os componentes; contexto privado ancestral fora da projeção permanece UNKNOWN. |
| OpenCode 2.2.5 | `parentID` sobrevive a exportação e reinício. A filha pode continuar independentemente e sua requisição usa o histórico próprio, sem injetar o histórico do pai. Importar com pai ausente falha. | Importar pai antes da filha; entregar histórico completo em cada nó. `parentID` representa organização, não prova limite de fork. |
| OpenCode 2.2.5, listagem | `/api/session` sem filtro inclui filhas; `parentID=null` retorna raízes; `parentID=<id>` retorna filhas. A listagem CLI de raízes não enumera a árvore inteira. | Descoberta futura precisa de API paginada ou manifesto explícito, com comprovação de completude. |

Provas reproduzíveis no repositório:

- [Fork persistente Claude](../tests/test_claude_nov_native.py), função
  `test_nov_stock_fork_persists_independent_transcript`.
- [Forks comum e anexado Codex](../tests/test_codex_nov_native.py), funções
  `test_nov_stock_persisted_fork_identity_and_inherited_read` e
  `test_nov_stock_attached_fork_is_persisted_and_hidden_from_ordinary_listing`.
- [Importação, reinício e continuação de filha OpenCode](../tests/test_opencode_v22_native.py),
  função `test_stock_225_server_import_api_and_persistent_parent`.

As capturas locais desta investigação estão em
`../validation-artifacts-051126/`, a partir da pasta pai do repositório:
`claude/outer-2.1.317/`, `claude/outer-2.1.318/`,
`codex/native-nov-fork-metadata.json`,
`codex/native-nov-attached-fork-metadata.json` e
`opencode/contract-summary.json`. Esses artefatos não são distribuídos pelo
esquema nem são necessários para compreender o exemplo sintético.

Referências primárias: [relato Claude Desktop #100216](https://github.com/anthropics/claude-code/issues/100216)
(conteúdo de usuário, não especificação oficial),
[metadados Codex 0.166.0](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/protocol/src/protocol.rs),
[importador OpenCode 2.2.5](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/core/src/session/transfer.ts)
e [schema de sessão OpenCode 2.2.5](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/schema/src/session.ts).
No importador, a checagem de conflito antecede a escrita e a consulta do pai
antecede a criação da sessão. Os testes acima estabelecem o comportamento
exercitado; não certificam outras versões, navegação visual na TUI ou conversas
reais do operador.

## Identidades e fatos de relação

A identidade de origem é a tupla exata
`(format, canonical_root, source_native_id)`. `profile_id` identifica a seleção
de perfil; a impressão do perfil detecta alteração de executável, ambiente ou
backend. Renomear um perfil não muda a identidade nativa. IDs iguais em roots
distintos não se fundem. O destino também exige root, perfil e testemunho de
identidade próprios, conforme [perfis](session-profiles.md).

A versão 1 admite exatamente um root de origem e um root de destino por
proposta. Relação que sair desse root exige erro explícito; não procurar a sessão
em outro HOME, perfil, banco ou root. Fixtures e exemplos não autorizam acessar
conversas reais. Os caminhos relativos de componentes devem ser canônicos e
únicos, permanecer no root e passar pelas proteções físicas de captura existentes.

O inventário contém somente metadados dos nós da árvore explicitamente
selecionada para investigação. `nodes[].persistent` exige evidência de
persistência; um nome de subagente ou `parent_thread_id` sozinho não satisfaz
esse requisito. `source_parent_status=unknown` permanece desconhecido.

`relationships` registra fatos com tipo, origem da evidência e referências:

- `organizational_parent`: vínculo organizacional explicitamente observado.
- `fork_origin`: origem de um fork persistente; não implica que o destino saiba
  continuar por prefixo nem que sua interface mostre o mesmo agrupamento.
- `provenance=native`: exige metadado capturado, campo específico, hash e versão
  de um adaptador aprovado. A string do nome de campo não basta.
- `provenance=manual`: exige declaração explícita, datada e identificada do
  operador. A proposta mostra que o vínculo foi declarado, sem apresentá-lo como
  metadado nativo recuperado.

Relatos externos podem ficar em `evidence`, mas não podem sustentar, sozinhos,
persistência, completude ou uma relação pronta para publicação. Nunca inferir
pai por título, pasta, horários próximos, igualdade de mensagens, `parentUuid`,
`isSidechain=false` ou um `session_id` compartilhado.

O conjunto `selection.hierarchy_relationship_keys` escolhe explicitamente quais
relações compõem a árvore organizacional que será projetada. Relações de fork
podem permanecer somente no registro privado. Projetar uma origem de fork em
`parentID` exige seleção explícita dessa relação e mantém o código de perda
`FORK_ORIGIN_RETAINED_IN_MIGRATION_METADATA_ONLY`: a semântica de fork não passou
a ser nativa no OpenCode.

## Seleção e invariantes que o JSON Schema não verifica

Os modos são `single`, `subtree` e `full_tree`. O usuário escolhe o nó âncora e o
modo; o sistema apresenta todos os IDs incluídos e as relações que serão
mantidas ou cortadas. Não expandir a seleção silenciosamente.

`single` inclui apenas a âncora. `subtree` inclui exatamente a âncora e todos os
descendentes no inventário declarado. `full_tree` inclui todos os nós a partir
da raiz; inventário parcial bloqueia esse modo. `declared_complete` afirma a
completude do manifesto manual, dentro de seu escopo, e não prova descoberta
completa no cliente. `verified_complete` exige adaptador e enumeração nativa
conclusiva, incluindo paginação. Não oferecer completude a partir de uma página
de raízes.

O validador semântico rejeita antes de qualquer escrita:

1. Chaves repetidas de nó, relação ou evidência; identidades nativas repetidas;
   referências ausentes; relações reflexivas; ciclos; mais de um pai na árvore
   selecionada; nós desconectados da raiz; profundidade acima do limite.
2. Relação nativa sem evidência aprovada, declaração manual não confirmada,
   conflito entre declaração e metadado, ou raiz declarada com pai observado.
   Pai desconhecido não se converte em raiz conhecida.
3. Seleção incompatível com o modo, âncora ausente ou conjunto incompleto de
   descendentes. Cada nó selecionado deve ter exatamente uma captura e um plano
   de destino; os demais não podem ter captura nem publicação nesta operação.
4. Filha selecionada cujo pai não foi selecionado, a menos que
   `boundary_policy=detach_explicit` e o plano da âncora registre
   `detached_selection_boundary`. Não fabricar pai vazio, importar ancestral sem
   seleção, conectar a sessão já existente ou transformar órfão em raiz.
5. `target_parent_node_key` divergente do pai selecionado, desconhecido ou não
   selecionado; `native_parent` sem relação correspondente; disposição
   `unsupported` enquanto o préflight está pronto. Uma relação mantida somente
   em registro usa `retained_only`, com o efeito visível e a perda apresentados.
6. Capturas alteradas, hashes inválidos, prefixo não resolvido, projeção parcial,
   título inválido, root/perfil divergente, colisão de ID ou capacidade excedida.

O grafo de hierarquia tem um pai por nó, sem ciclos: uma árvore dentro de um DAG
de fatos. Mais de um fato para o mesmo par, de tipos diferentes, não cria dois
pais. Subárvores selecionadas podem cortar somente a fronteira declarada; não
podem conter órfãos internos.

## Histórico, IDs, títulos e privacidade

`captures` referencia snapshots imutáveis, componentes relativos ao root,
revisões e testemunhos canônicos. Parser e auditor consomem os mesmos bytes,
como em [checkpoint primitives](lineage-checkpoints.md). O hash de um snapshot
multiarquivo usa a política proposta `tree-source-components-v1`: componentes
ordenados pelo UTF-8 de `relative_path`, cada um com caminho, quantidade de bytes
e SHA256 dos bytes realmente capturados. `snapshot_sha256` é o SHA256 do JSON
canônico de `{"policy":"tree-source-components-v1","components":[...]}`.
Conferir os hashes contra os bytes e manter esses bytes imutáveis para parser e
auditor continua obrigatório; uma lista de hashes declarados não é uma captura.
Os hashes sintéticos do exemplo são deliberadamente inválidos como prova dessa
política, mesmo tendo o formato hexadecimal correto.

O histórico de cada destino é materializado independentemente. Se o arquivo da
filha já contém o prefixo copiado, ele é emitido uma vez no histórico dessa
filha; não concatenar o pai novamente. Repetir contexto compartilhado em duas
sessões independentes é esperado. Remover texto parecido ou procurar uma
subsequência pode apagar conversa válida e é proibido.

Prefixo referenciado exige resolução explícita e limitada dos componentes,
testemunho de unidades e links de ferramenta, ordem e limite nativo comprovado.
Uma relação `fork_origin` não resolve um `history_base`. Prefixo sem prova de
fechamento, ferramenta pendente, ciclo de referência ou dependência fora do
root bloqueia a materialização. `shared_prefix=null` significa ausência de
testemunho, não prefixo vazio nem autorização para deduplicar. O modo atual
conservador do leitor Codex continua válido até existir um adaptador específico.

A estratégia proposta para novos IDs é
`uuid5_operation_qualified_source_v1`: usar `operation_id` como namespace UUIDv5
e como nome o JSON UTF-8 compacto da lista
`[format, canonical_root, source_native_id]`, com Unicode preservado, sem espaços
e sem normalização adicional. O ID OpenCode é `ses_` seguido dos 32 dígitos
hexadecimais desse UUID. A proposta fixa esses IDs antes do préflight; o diário
os reutiliza para inspeção. Nova operação gera outro namespace. Colisão bloqueia
a operação inteira; não sobrescrever, renomear silenciosamente ou reaproveitar
uma sessão preexistente.

Cada plano possui título solicitado e modo explícito (`native` ou `local_alias`),
conforme [títulos](target-titles.md). Validar UTF-8, controles e limite de 1024
bytes; o limite de caracteres do JSON Schema não substitui a validação em bytes.
Verificar separadamente título, `parentID`, identidade e histórico no destino.

Proposta e diário contêm metadados, hashes e referências; não carregam corpos de
conversa, argumentos de ferramenta ou mídia. São arquivos privados porque roots
e títulos ainda podem identificar projetos. Não copiar permissões nativas,
credenciais, contas, políticas, MCPs, hooks, providers ou ambiente da origem.
Construir a entrada nativa por lista explícita de campos permitidos: o
importador OpenCode aceita `permissions`, portanto exportar/importar JSON bruto
da origem não é um atalho permitido.

## Préflight e publicação

`preflight.status=ready_for_live_review` é uma descrição, nunca autorização
reaproveitável. Carregar um JSON salvo não reconstitui um handle de publicação.
`synthetic=true` bloqueia publicação independentemente do status declarado.

O préflight prepara **todos** os nós selecionados antes de publicar o primeiro:
valida inventário, grafo, evidência, captura, projeção, títulos, IDs, root de
origem, backend/root de destino, disponibilidade do importador e capacidade do
diário. Planeja a ordem topológica, pai antes de filha, e a remapagem integral de
`parentID`. Não cria sessão nativa, manifesto, alias nem arquivo do destino.
O recibo de préflight cobre a proposta canônica e seus testemunhos.

Imediatamente antes de cada nó, revalidar namespace, hash da origem, ausência de
colisão e pai já verificado. Mutações do cliente nativo podem acontecer após o
préflight: locks do migrador não são locks do cliente. Mudança de identidade ou
conteúdo exige nova preparação; não resolver corrida sobrescrevendo dados.

A publicação utiliza o protocolo de [migration records](migration-records.md),
com registro coordenador da árvore e um registro durável por nó. Uma árvore não
é uma transação distribuída atômica. Cada evento obedece ao
`$defs/journalEvent` do esquema; a implementação deve integrá-lo ao ledger
privado existente, sem criar um segundo publicador que contorne esse protocolo.

| Estado por nó | Evidência e próximo passo |
| --- | --- |
| `reserved` | ID reservado no ledger; nada prova a existência da sessão. |
| `intent_durable` | Intenção persistida e sincronizada antes da chamada nativa; marcar `attempted_unknown` conservadoramente. |
| `native_acknowledged` | Importador confirmou o ID; ainda falta exportação independente. |
| `verified` | Exportação nova confirmou ID, pai, título e projeção esperada. |
| `completed` | Recibo e coordenação da árvore persistidos. |
| `failed_before_publish` | Importador não foi chamado; `native_not_started` comprovado. |
| `needs_reconcile` | Chamada incerta, verificação falhou ou finalização falhou; bloquear as próximas publicações. |

Eventos usam sequência monotônica e hashes encadeados, com persistência durável
antes de avançar. `event_sha256` cobre o evento canônico exceto seu próprio
campo; `previous_event_sha256=null` somente no primeiro evento do nó. `verified`
e `completed` exigem exportação observada e recibo; transições regressivas e
combinações incoerentes de `stage`/`publication` são inválidas. O coordenador
mantém a proposta e o mapa de IDs, além do resultado individual de todos os nós.
Para esses dois hashes documentais, JSON canônico significa serializar com
chaves ordenadas, Unicode preservado, sem espaços ou newline, sem `NaN`/infinito,
e codificar UTF-8: `json.dumps(value, sort_keys=True, ensure_ascii=False,
separators=(",", ":"), allow_nan=False).encode("utf-8")`. Não normalizar strings;
o esquema admite somente inteiros no intervalo exato declarado. A política de
hash canônico de conversa continua sendo a existente e é identificada
separadamente em `canonical_policy`.

Falha ou cancelamento interrompe novos nós após finalizar o trecho de publicação
em andamento. O resultado distingue concluídos, não iniciados e incertos. Não
apagar pai ou filha como rollback. Recuperação reabre somente para inspecionar
metadados e exportações conhecidas; não executa proposta salva, não reimporta
automaticamente e não considera ausência numa listagem de raízes prova de
ausência da filha. Continuar uma operação parcial exige reconciliação explícita,
novo préflight e autorização de execução vigente, preservando os IDs já fixados.

Limites iniciais: 1024 nós, profundidade 64, 2048 fatos de relação, 256 MiB por
captura, 1 GiB de capturas totais, JSON da proposta de até 8 MiB e profundidade
JSON 128. Aplicar limite de bytes antes de decodificar e limites de grafo de forma
iterativa. Não truncar histórico, lista de nós ou evidência para caber. O ledger
existente mantém seus próprios limites e deve reservar espaço para finalizar
todos os nós e registrar uma falha; excedê-los bloqueia o préflight.

## Etapas e critérios de aceite

1. **Identificar a origem Claude concreta.** Confirmar interface, versão, OS e
   ação que cria a filha. Produzir árvore sintética de pelo menos três níveis,
   continuar cada nó após reinício e capturar apenas seus metadados e históricos
   sintéticos. Para Desktop, ligar o arquivo de metadado ao transcript exato e
   testar `lineageDetached`. Até lá, só há manifesto manual explícito; não há
   autodescoberta Claude Desktop aprovada.
2. **Implementar modelo e preparação sem escrita.** Parser JSON sem dependência
   nova, validação semântica, identidade qualificada, seleção, fechamento,
   testemunhos e IDs estáveis. Testar pai duplicado, ciclo, órfão, identidade
   repetida em mesmo root, IDs iguais em roots diferentes, limite e cancelamento.
3. **Adicionar adaptador nativo apenas com contrato comprovado.** Metadado
   desconhecido ou conflito é erro explícito. Testar metadados ausentes,
   destacamento, várias filhas, neta e origem inacessível. Declaração manual deve
   permanecer identificada em proposta e recibo.
4. **Implementar publicação topológica OpenCode 2.2.5.** Préflight global,
   diário coordenador, registros por nó, recaptura e ausência de sobrescrita.
   Injetar falhas antes/depois de cada persistência, importação, verificação e
   finalização; comprovar que reiniciar não chama o importador automaticamente.
5. **Provar o ciclo nativo completo.** Importar pai, duas filhas e neta; reiniciar
   servidor; enumerar pela API com paginação; exportar cada ID; verificar arestas,
   títulos, contexto próprio, ferramentas e prefixos; continuar cada filha com
   provider de loopback e demonstrar que não usa histórico exclusivo do pai nem
   da irmã. Testar seleção única, subárvore destacada e árvore inteira. Confirmar
   que nenhum arquivo de origem mudou e nenhuma permissão/credencial foi copiada.
6. **Integrar apresentação e revisão.** Exibir árvore, seleção, fatos manuais,
   perdas, IDs e resultado parcial. A navegação visual de filhas na TUI do cliente
   é uma prova separada; uma API que lista filhas não garante essa experiência.

Critério final: todos os nós selecionados e somente eles são encontrados no
root/backend escolhido, com IDs novos fixados, relações projetadas verificadas,
históricos independentes equivalentes à projeção aprovada e diário concluído.
Qualquer nó incerto torna o resultado parcial. A DEV16 implementa os marcos de runtime para inventários explícitos e contratos
nativos comprovados, incluindo testes de quatro nós em standalone e servidor conectado.
A descoberta automática de metadados privados Claude Desktop continua dependente de
uma fixture/contrato próprio; o manifesto manual cobre essa origem funcionalmente.
O diário real acrescenta `attempting.target_sha256` por nova preparação e títulos
por nó; nunca reexecuta nós incertos ou promove cobertura ausente.
