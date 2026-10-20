# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio "A Ponte" — duas pontas funcionando e verificáveis:

- **`servidor-mcp/`** — servidor MCP em Streamable HTTP (revisão 2026-07-28 da spec), porta **7301**, endpoint `/mcp`. Três tools, um resource, os dois tipos de erro e o ciclo completo de MRTR (`input_required`) na reserva.
- **`agente/`** — o mesmo agente com triplo papel: **host MCP** por dentro (descobre e chama as tools do servidor por HTTP) e **servidor A2A v1.0** (binding JSON-RPC) por fora, porta **7300**, endpoint `/a2a` e Agent Card em `/.well-known/agent-card.json`.

Sem LLM no caminho de execução: o pedido chega em formato fixo e o agente decide por regra (determinístico).

## Como rodar

Pré-requisitos: Python 3.10 ou superior (validação feita com 3.14). Nenhuma dependência além do SDK oficial (`mcp`), travado em `requirements.txt` e nos `pyproject.toml` de cada processo.

```bash
# a partir de um clone limpo do repositório
cd desafio-a2a-com-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Segredo de integridade do requestState (mínimo 32 bytes de aleatoriedade).
# Gere o SEU — nunca reutilize nem publique o valor:
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

```bash
# terminal 1 — servidor MCP (porta 7301), com o stderr visível:
.venv/bin/python servidor-mcp/servidor.py

# terminal 2 — agente (porta 7300):
.venv/bin/python agente/agente.py

# validador do desafio (36 verificações):
.venv/bin/python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

O `REQUEST_STATE_SECRET` precisa estar exportado no ambiente do **servidor MCP** (é a chave que sela o `requestState`; o servidor recusa subir sem ela). As portas podem ser trocadas por variável de ambiente (`MCP_PORT`, `AGENT_PORT`, `MCP_URL`), mas os padrões são 7301/7300, como o validador espera.

## Onde a ponte acontece

A ponte é a costura entre o `input_required` do MRTR do MCP e a Task do A2A — nenhum dos dois protocolos tem sessão, então o estado viaja explicitamente:

1. **No servidor MCP** (`servidor-mcp/servidor.py`), o resolver `escolha_de_sala` roda antes do corpo da tool `reservar_sala`. Quando o intervalo está ocupado e há alternativas, ele devolve um `Elicit(...)` (elicitation em form mode, com o enum das salas alternativas na ordem da regra) e o framework **encerra a resposta** com `resultType: "input_required"` — o servidor não inicia request, ele termina a resposta pedindo informação. O `requestState` (com o pedido original selado) é produzido na borda e o servidor **não guarda nada em memória** entre o `input_required` e o retry.
2. **No agente** (`agente/agente.py`, função `_pausar`): o `input_required` chega **cru** (o cliente MCP usa `allow_input_required=True`; nunca há callback de elicitation respondendo sozinho). O agente coloca a Task em `TASK_STATE_INPUT_REQUIRED` com a linha exata `alternativas: <ids>` e guarda o `requestState` **ligado àquela Task** — opaco, nunca exposto no card, no artifact nem em mensagem ao cliente A2A.
3. **Na retomada** (`agente/agente.py`, função `_continuar_task`): o `SendMessage` de continuação (`escolha=<sala>` ou `escolha=recusar`) repete o `tools/call` original com um **id de JSON-RPC novo** (cada chamada cunha um id próprio), levando `inputResponses` com a **mesma chave** que veio no `inputRequests` e o `requestState` ecoado **sem modificação**. O servidor verifica a integridade na borda, reconstrói o pedido a partir do próprio `requestState` e conclui a reserva.

## Decisões técnicas

- **Proteção do requestState:** `RequestStateSecurity` do SDK (`mcp==2.3.0`) — **AES-256-GCM** com chave derivada por HKDF-SHA256 de `REQUEST_STATE_SECRET` (≥ 32 bytes, nunca hardcoded). **TTL de 600 segundos** (dentro da faixa de 5–30 min), audience = nome do servidor. Como tudo que o servidor precisa viaja no token, **o retry funciona mesmo após o processo do servidor MCP ser reiniciado**; um `requestState` adulterado é rejeitado com `-32602`.
- **Integridade das rodadas:** o framework sela/verifica o envelope a cada rodada, com binding ao método, ao nome da tool e ao digest dos argumentos — um retry com argumentos divergentes do pedido selado é recusado na borda (a divergência não toma efeito).
- **Estado das Tasks:** em memória, no processo do agente (`dict` + lock). O estado pausado é **por Task**: cada Task carrega o seu próprio `requestState`, então duas pausas simultâneas não se misturam. Reservas vivem em memória no servidor MCP (não sobrevivem a restart; o `requestState` sim).
- **Cliente MCP por request:** o agente abre uma conexão Streamable HTTP por mensagem A2A e sempre faz `tools/list` antes do primeiro `tools/call` (descoberta em runtime — sem lista fixa no código) e lê o resource `politica://uso` para extrair a versão. O `_meta` de cada request carrega `protocolVersion`, `clientInfo`, `clientCapabilities` (incluindo `elicitation.form`) e o `traceparent` propagado do cliente A2A (mesmo trace-id, span novo).
- **Capability de elicitation:** declarada via callback de elicitation registrado no cliente MCP — que nunca responde pelo cliente A2A, porque o fluxo consome o `input_required` cru.
- **Recusa:** `escolha=recusar` vira `action: "decline"`; o servidor conclui sem reservar (`reservado: false` + `motivo`, sem `isError`) e a Task termina em `TASK_STATE_CANCELED`.
- **Determinismo:** pedido em formato fixo (`reservar sala=... inicio=... fim=... responsavel=...`), decisão por regra. Alternativas: salas livres no intervalo com capacidade ≥ a pedida, no máximo 3, ordenadas por capacidade crescente e `id` alfabético em empate.
- **Log no stderr:** um middleware registra cada request recebido com método, id e `traceparent` (quando vem no `_meta`) — é o instrumento de depuração do avaliador.

## Saída do validador

Última execução (processos recém-iniciados), **exit code 0**:

```text
trace-id desta execucao: 852f8ee2872d980268f15e82bf110776
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

Nos logs do servidor MCP (stderr) dá para conferir o roteiro completo: o `tools/list` do agente antes do primeiro `tools/call`, o `traceparent` com o trace-id impresso pelo validador, e o par de `tools/call` de uma reserva que passou pela pausa — o retry com id diferente do request inicial.

## Estrutura do repositório

```text
.
├── README.md              (este arquivo; o enunciado original está em
│                           https://github.com/devfullcycle/desafio-a2a-com-mcp)
├── requirements.txt       (mcp==2.3.0)
├── dados/                 (não alterado)
├── validador/             (não alterado)
├── exemplos/              (não alterado)
├── servidor-mcp/
│   ├── servidor.py        (servidor MCP: tools, resource, MRTR, stderr)
│   └── pyproject.toml     (versões travadas)
└── agente/
    ├── agente.py          (host MCP + servidor A2A + a ponte)
    └── pyproject.toml     (versões travadas)
```
