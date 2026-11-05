# Migrar uma árvore de sessões

Disponível na DEV16: selecionar uma sessão, uma subárvore ou a árvore inteira e
publicar sessões novas no OpenCode **2.2.5**, com títulos e vínculos `parentID`.
O pai é importado antes das filhas. O histórico próprio de cada nó é convertido
uma vez; um prefixo que já veio copiado no fork não é concatenado de novo.

## Entrada e perfis

Use os [perfis privados](session-profiles.md) existentes: um perfil de origem
com root explícito e outro OpenCode com CLI e root de destino. O arquivo de
perfis precisa ser privado (diretório 0700, arquivo 0600). Na CLI, os exemplos
abaixo usam IDs de perfil `claude-source` e `opencode-target`.

Os vínculos do Claude CLI não são deduzidos de `parentUuid`, títulos ou texto.
Quando a origem não registra o pai, declare as relações num
[manifesto de inventário](examples/session-tree-inventory-v1.json). Substitua os
IDs, caminhos relativos, títulos e declaração pelos seus dados. A entrada segue
este [schema de runtime](schemas/session-tree-inventory-v1.schema.json); cada ID
é conferido nos bytes do arquivo dentro do root escolhido. A declaração é
proveniência manual e continua identificada assim na revisão. Versões Claude
conhecidas podem coexistir no mesmo transcript; a identidade é conferida sem
promover a cobertura UNKNOWN de um histórico com versões mistas.

A descoberta nativa usa `forked_from_id` no Codex e `parentID` em exports públicos
OpenCode. Inventário offline é parcial em relação ao cliente. Claude Desktop
continua sem decoder especulativo de metadados privados: use o manifesto
explícito para migrar sua família de sessões. Nenhuma heurística de similaridade
transforma conversas independentes em filhas.

```bash
smigrate tree inventory --profiles /absolute/private/profiles.json \
  --source-profile claude-source --source-client-version 2.1.318 \
  --manifest /absolute/private/family.json

smigrate tree review --profiles /absolute/private/profiles.json \
  --source-profile claude-source --source-client-version 2.1.318 \
  --manifest /absolute/private/family.json --target-profile opencode-target \
  --mode full_tree --cwd /absolute/project --model-provider anthropic --model MODEL_ID
```

Confira os nós, IDs novos, títulos, relações, perdas e `coverage`. A publicação
requer revisão atual e confirmação; o JSON exportado não é executável:

```bash
smigrate tree migrate --profiles /absolute/private/profiles.json \
  --source-profile claude-source --source-client-version 2.1.318 \
  --manifest /absolute/private/family.json --target-profile opencode-target \
  --mode full_tree --cwd /absolute/project --model-provider anthropic --model MODEL_ID \
  --acknowledge-partial
```

Em terminal interativo, digite `yes` após revisar. `--yes` é a confirmação
explícita para execução não interativa. Os parâmetros de provider/modelo
configuram o destino; a migração não faz chamadas a um modelo.

Para uma subárvore, use `--mode subtree --anchor child`. Se o pai ficar fora da
seleção, acrescente `--detach` para destacar a âncora explicitamente. Para um só
nó, use `--mode single --anchor child` com a mesma regra de fronteira. A operação
não inclui ancestrais silenciosamente e não sobrescreve sessões existentes.

## TUI e dashboard

Abra `smigrate tui` e use **Ctrl+T** ou o botão de árvore. Informe os perfis e o
inventário, leia a árvore visual, selecione a âncora, escolha o modo e revise a
proposta. Selecionar outra âncora invalida a proposta anterior. A confirmação é
usada uma única vez; cancelar durante publicação mantém o resultado visível.

No dashboard opcional, acrescente `--allow-tree --allow-publish` aos argumentos
do seu lançamento `smigrate web`, junto dos perfis/projetos já selecionados.
A seção de árvore recebe o manifesto, usa somente esses perfis/projetos e passa
pelas mesmas autenticação, nonce e confirmação única. Sem as duas opções, a
publicação de árvores é recusada. O Web limita a 16 nós, 4 MiB por fonte e 8 MiB
agregados; a TUI respeita seu orçamento de leitura. Exceder limites gera erro,
sem truncamento de histórico.

## Resultados parciais e retomada

O diário fica no state home privado do migrador, com um coordenador da árvore e
operações individuais no ledger existente. A árvore não é uma transação atômica.
Um erro interrompe novos nós e preserva os já publicados; não há rollback por
exclusão nem repetição automática de importação incerta.

```bash
smigrate tree inspect OPERATION_UUID
smigrate tree reconcile OPERATION_UUID
```

`inspect` lê o diário; `reconcile` inspeciona a situação sem importar nem
autorizar repetição automática. Para continuar
nós comprovadamente não iniciados, execute `tree resume` com os mesmos argumentos
de seleção/destino e `--operation-id OPERATION_UUID`, revise e confirme novamente.
Nós concluídos são conferidos contra os ledgers reais, fonte, opções, IDs, hashes,
pais, CWD e backend. Mudança de um nó concluído interrompe a retomada.

Cada nova tentativa de um nó ainda não iniciado registra seu novo `operation_id`
e `target_sha256` antes de chamar o importador. Isso permite várias interrupções
limpas sem reaproveitar hashes antigos de IDs internos gerados pelo writer.
Nós incertos permanecem `needs_reconcile`; seu importador não é chamado de novo.
Cobertura ausente em registros antigos permanece `unknown`, sem inventar perdas
zero ou promover confiança.

## Codex comprimido e histórico compartilhado

Leitura, catálogo, inspeção e prévia aceitam `.jsonl.zst` e resolvem
`history_base` no mesmo root. O ID físico do rollout é separado do ID lógico da
sessão. Byte offsets, ordinais, cutoffs de fork/revert, ciclos, arquivos duplicados
e componentes que mudaram são validados antes da migração.

Python 3.14 usa `compression.zstd`; Python 3.11–3.13 usa a biblioteca `libzstd`
do sistema com streaming limitado. Não há nova dependência Python obrigatória.
Sem um decoder compatível, a leitura falha explicitamente. Entrada truncada,
chaves JSON duplicadas e expansão acima do orçamento não produzem histórico parcial.

Hashes físicos, bytes decodificados e a autoridade composta são separados.
Parser e auditor consomem a mesma captura imutável. A resolução de vários
componentes mantém `coverage=unknown` para contextos privados dos headers
ancestrais fora da projeção. Isso permite revisar o histórico visível sem
alegar preservação integral de estado privado, custos, checkpoints ou janela
nativa. Vínculo `parentID` organizacional também não prova fronteira de fork.
