# Changelog do fork

Histórico das melhorias de `TheViniAlmeida/session-migrate`. As datas usam
UTC−03:00 (America/Sao_Paulo). A integração é registrada pelo dia do merge na
branch `next`; desenvolvimento e integração podem ocorrer em dias diferentes.

O histórico do projeto original está em [CHANGELOG.md](CHANGELOG.md).
Versões `.dev` são pré-releases do fork: integração na `next` não significa
publicação no PyPI nem atualização automática das instalações existentes.

## 11/10/2026

- Adicionado este histórico datado das melhorias do fork, com referências às
  PRs, versões, validações e limitações.
- Adicionado link para este arquivo no README.
- Instalada e validada a `0.12.0.dev9` com extras `tui` e `web`, primeiro no
  usuário operador em ambiente isolado e depois na instalação compartilhada.
- Instalação do operador: 16 distribuições e 83 arquivos conferidos,
  **197 testes passaram sem skips**, além do fluxo de navegador privado.
- Instalação compartilhada: **sete contas validadas**, com 42 verificações de
  CLI, sete imports de core/TUI/web, `pip check`, migração e recuperação
  sintéticas, TUI por teclado e fluxo de navegador. Os nove runtimes anteriores
  e o pip compartilhado permaneceram intactos; somente seis dependências web
  foram acrescentadas.
- Corrigida a validação dos executáveis internos declarados pelos wheels
  `idna` e `uvicorn`: caminhos relativos do RECORD passaram a ser normalizados.
  A etapa parcial foi concluída com backup do estado real e sem reinstalar
  pacotes, mudar suas permissões ou apagar o journal da tentativa anterior.
  Esses executáveis internos não foram publicados como comandos globais extras.
- Backups restaurados para conferência, revisão independente pós-instalação
  aprovada e servidores de teste encerrados. Nenhum perfil, autenticação ou
  conversa real dos harnesses foi usado nos testes.

## 10/10/2026 — integração das melhorias

| PR | O que foi entregue | Versão de desenvolvimento |
| --- | --- | --- |
| [#1](https://github.com/TheViniAlmeida/session-migrate/pull/1) | Integração da spec e do plano de implementação das fases 2–6, escritos em 09/10. | — |
| [#2](https://github.com/TheViniAlmeida/session-migrate/pull/2) | Core compartilhado de operações, preparação e execução de migrações. | — |
| [#3](https://github.com/TheViniAlmeida/session-migrate/pull/3) | Correções do adapter OpenCode 2: ordenação de ferramentas/resultados e validação dos campos nativos. | — |
| [#4](https://github.com/TheViniAlmeida/session-migrate/pull/4) | Perfis isolados de origem/destino e capabilities por harness. | — |
| [#5](https://github.com/TheViniAlmeida/session-migrate/pull/5) | TUI opcional para escolher perfis, sessões e destino, visualizar a conversa e confirmar a migração. | `0.12.0.dev1` |
| [#6](https://github.com/TheViniAlmeida/session-migrate/pull/6) | Correção da corrida entre edição/cancelamento da TUI e publicação: conservação do resultado e do recibo quando a publicação já começou. | `0.12.0.dev2` |
| [#7](https://github.com/TheViniAlmeida/session-migrate/pull/7) | Nome de destino nativo quando comprovadamente suportado, alias local explícito e propostas de uso único revalidadas na confirmação. | `0.12.0.dev3` |
| [#8](https://github.com/TheViniAlmeida/session-migrate/pull/8) | Ledger privado e durável de migrações, locks e registros de linhagem. | `0.12.0.dev4` |
| [#9](https://github.com/TheViniAlmeida/session-migrate/pull/9) | Checkpoints exatos e capturas imutáveis da origem, com hashes e evidências para comparação. | `0.12.0.dev5` |
| [#10](https://github.com/TheViniAlmeida/session-migrate/pull/10) | Integração dos registros duráveis à migração e verificação dos imports após o reconhecimento nativo. | `0.12.0.dev6` |
| [#11](https://github.com/TheViniAlmeida/session-migrate/pull/11) | Avaliação de continuidade e recuperação explícita de operações incertas, sem repetir automaticamente o import. | `0.12.0.dev7` |
| [#12](https://github.com/TheViniAlmeida/session-migrate/pull/12) | Handoff editável e retorno em nova sessão pela CLI/TUI, vinculados à origem atual e à transferência anterior. | `0.12.0.dev8` |
| [#13](https://github.com/TheViniAlmeida/session-migrate/pull/13) | Dashboard web opcional, observações de uso, handoff/retorno, inspeção de operações e serviços de recuperação habilitados explicitamente. | `0.12.0.dev9` |

### Handoff e retorno

- Seleção explícita de trechos, edição integral do documento e conferência de
  SHA-256, sem cortar ou normalizar o texto aceito.
- Handoff registrado como contexto fornecido pelo usuário, com proveniência
  derivada, sem alegar preservação integral do histórico.
- Retorno usa a sessão atual, incluindo trabalho realizado depois da migração,
  e cria uma nova identidade vinculada à transferência anterior.
- Provas nativas executadas com Claude Code `2.1.209`, Codex `0.144.4` e
  OpenCode `2.0.22`/`2.0.23`, em ambientes privados com dados sintéticos.

### Dashboard opcional

- Seleção de perfis, sessões e projetos, previews literais, nome de destino,
  revisão e confirmação da transferência.
- Edição de handoff, retorno em nova sessão e acompanhamento das operações.
- Observações de tokens com proveniência; valores não comprovados de totais,
  custo e percentual de contexto permanecem desconhecidos.
- Autenticação limitada à execução, listener em loopback e modo somente
  leitura por padrão. Publicação, handoff, retorno, adoção de baseline,
  reconciliação e cancelamento de operações READY exigem habilitação explícita.
- Propostas vinculadas à execução e confirmação idempotente; guards nativos
  permanecem ativos após o início da publicação.
- Assets HTML/CSS/JavaScript empacotados e dependências web opcionais, mantendo
  o core sem dependências de runtime.
- Testes de TUI ajustados para aguardar o carregamento real dos perfis antes
  de enviar Ctrl+H. O bloqueio do produto durante trabalho pendente foi mantido.

### Validações registradas na entrega DEV9

- Suíte integrada local: **3.753 testes passaram**, com **82 testes pulados**
  por falta dos pré-requisitos de clientes externos nessa rodada.
- Pacote instalado: **104 testes passaram**, sem falhas ou skips.
- CI da PR13: **30 checks aprovados**, incluindo web em Python 3.11, 3.12 e 3.13.
- Navegador privado com sandbox: transferência, handoff editável, publicação,
  uso, inspeção de operações, logout, rejeição de replay e layout móvel.
- Wheel conferido contra os **83 arquivos de código e assets** da versão
  revisada e da árvore final integrada.

### Limites da DEV9

- Atualização da mesma identidade de sessão permanece desabilitada. As provas
  de concorrência em Claude Code `2.1.209` e Codex `0.144.4` mostraram que o
  lock privado do migrador não impede escritas de outro processo nativo;
  exclusão ou CAS seguro não foram demonstrados nos caminhos testados.
- Handoff para OpenCode `2.0.22`/`2.0.23` tem cobertura de destino **PARTIAL**,
  pois o importer adiciona uma variante de modelo. Retorno integral para
  OpenCode é bloqueado antes de criar uma nova operação READY.
- A entrega não habilita envio de documentos para modelos externos pelo
  dashboard nem inventa custos, totais nativos ou janela de contexto.
- Preparação e revisão do instalador não equivalem à atualização compartilhada:
  aplicação exige autenticação de administrador, backup recente verificado e
  validação da instalação efetiva. A inclusão do extra web é opcional.

## 09/10/2026 — início do fork

- Implementado suporte versionado às transferências de sessões OpenCode 2,
  mantendo o adapter legado e os controles de dry-run e não sobrescrita.
  Referência: commit
  [662976e](https://github.com/TheViniAlmeida/session-migrate/commit/662976e).
  A correção também foi encaminhada ao upstream na
  [PR17](https://github.com/xhluca/session-migrate/pull/17).
- Escritas a [spec](docs/specs/interactive-migration.md) e o
  [plano de implementação](docs/plans/interactive-migration-implementation.md)
  das fases 2–6, antes da implementação. Referência: commit
  [d161bfd](https://github.com/TheViniAlmeida/session-migrate/commit/d161bfd),
  integrado em 10/10 pela PR1. A instalação da fase 1 ficou fora desses documentos.
