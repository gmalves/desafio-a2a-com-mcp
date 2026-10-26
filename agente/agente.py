#!/usr/bin/env python3
"""Agente A2A da Central de Salas (Hill Valley Tech).

Desafio "A Ponte" - as duas pontas:

- POR DENTRO (host MCP): descobre as tools por tools/list, le o resource
  politica://uso, chama reservar_sala por HTTP como cliente MCP de verdade, e
  ENXERGA o input_required cru (nunca responde a elicitation sozinho);
- POR FORA (servidor A2A v1.0, binding JSON-RPC): Agent Card no well-known,
  SendMessage e GetTask, Task com identidade/estado/produto;
- A PONTE: ao receber input_required do MCP, a Task vai para
  TASK_STATE_INPUT_REQUIRED com a linha "alternativas: ...", o requestState
  fica guardado (opaco, ligado a Task e nunca exposto) e a continuacao
  "escolha=<sala>" repete o tools/call com id de JSON-RPC NOVO - o agente cunha
  o id de cada tools/call (_DispatcherComIdsProprios), entao o retry nunca
  repete o id do request inicial, devolvendo inputResponses com a MESMA chave e
  o requestState ecoado sem modificacao.

Sem LLM no caminho de execucao: o pedido chega em formato fixo e a decisao e
por regra. Determinismo garantido.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from mcp import Client
from mcp_types import ElicitResult, Implementation, InputRequiredResult

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
PORTA = int(os.environ.get("AGENT_PORT", "7300"))

CLIENT_INFO = Implementation(name="agente-central-de-salas", version="1.0.0")

ESTADOS_TERMINAIS = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED"}


def _novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


_ID_DE_CHAMADA = 4
"""Proximo id de JSON-RPC do tools/call (sequencia unica do processo).

O primeiro tools/call de uma sessao nova e o quarto request (server/discover=1,
tools/list=2, resources/read=3, tools/call=4): comecar em 4 mantem o log igual ao
de um cliente que continua contando uma sessao so.
"""
_TRAVA_CHAMADAS = threading.Lock()


def _proximo_id_de_chamada() -> int:
    """Cunha o id do proximo tools/call.

    O retry precisa sair com um id de JSON-RPC diferente do request original.
    Como o agente abre uma conexao por mensagem A2A, a contagem do dispatcher
    recomeca a cada mensagem e o tools/call cairia sempre no mesmo id; a
    sequencia unica do processo garante o id novo em qualquer ordem de conexoes.
    """
    global _ID_DE_CHAMADA
    with _TRAVA_CHAMADAS:
        id_chamada = _ID_DE_CHAMADA
        _ID_DE_CHAMADA += 1
        return id_chamada


class _DispatcherComIdsProprios:
    """Dispatcher decorator: todo tools/call sai com um id cunhado pelo agente.

    O dispatcher do SDK numera os requests por sessao (server/discover=1,
    tools/list=2, resources/read=3, tools/call=4), entao uma conexao nova por
    mensagem A2A faria o retry reusar o id=4 do request inicial da mesma reserva.
    O decorator injeta CallOptions["request_id"] - o id que o caller cunha e que o
    dispatcher honra verbatim, mantendo-o fora do proprio contador - com um valor
    da sequencia do processo. O resto do dispatcher (run/notify/estado) passa
    direto: os requests que nao sao tools/call seguem com o id da sessao.
    """

    def __init__(self, interno: Any) -> None:
        self._interno = interno

    def __getattr__(self, nome: str) -> Any:
        return getattr(self._interno, nome)

    async def send_raw_request(
        self, metodo: str, params: Any, opts: Any = None, **extra: Any
    ) -> dict[str, Any]:
        if metodo == "tools/call":
            opts = dict(opts or {})
            opts.setdefault("request_id", _proximo_id_de_chamada())
            print(
                f"[agente] request method={metodo} id={opts['request_id']}",
                file=sys.stderr,
                flush=True,
            )
        return await self._interno.send_raw_request(metodo, params, opts, **extra)

    async def notify(self, metodo: str, params: Any, opts: Any = None, **extra: Any) -> None:
        return await self._interno.notify(metodo, params, opts, **extra)


def _card() -> dict[str, Any]:
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": "1.0.0",
        "supportedInterfaces": [
            {"url": f"http://127.0.0.1:{PORTA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    }


class Task:
    """Estado de uma Task: identidade, estado e produto. O requestState pausado
    vive aqui, ligado a Task - e nunca sai em resposta A2A."""

    def __init__(self, task_id: str, context_id: str) -> None:
        self.id = task_id
        self.context_id = context_id
        self.state = "TASK_STATE_SUBMITTED"
        self.mensagem: str = ""
        self.history: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        # Estado da ponte (por Task):
        self.args: dict[str, Any] | None = None
        self.trace_id: str | None = None
        self.chave: str | None = None
        self.alternativas: list[str] = []
        self.request_state: str | None = None

    def add_history(self, texto: str, papel: str, message_id: str) -> None:
        msg: dict[str, Any] = {"messageId": message_id, "role": papel, "parts": [{"text": texto}]}
        if papel == "ROLE_USER":
            if self.history:  # continuacao referencia a Task
                msg["taskId"] = self.id
        else:
            msg["taskId"] = self.id
            msg["contextId"] = self.context_id
        self.history.append(msg)

    def dict(self) -> dict[str, Any]:
        status_msg = {
            "messageId": _novo_id("msg"),
            "role": "ROLE_AGENT",
            "parts": [{"text": self.mensagem}],
            "taskId": self.id,
            "contextId": self.context_id,
        }
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": {"state": self.state, "message": status_msg},
            "history": self.history,
            "artifacts": self.artifacts,
        }


TAREFAS: dict[str, Task] = {}
TRAVA = threading.Lock()


def _trace_meta(traceparent: str | None, fallback_trace: str | None = None) -> dict[str, Any] | None:
    """Mesmo trace-id do cliente A2A; span-id pode ser novo (spec M6)."""
    trace_id = None
    if traceparent:
        partes = traceparent.strip().split("-")
        if len(partes) >= 4 and len(partes[1]) == 32:
            trace_id = partes[1]
    trace_id = trace_id or fallback_trace
    if not trace_id:
        return None
    return {"traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"}


async def _elicitation_nao_usada(*args: Any, **kwargs: Any) -> ElicitResult:
    """Callbacks de elicitation declaram a capability no cliente MCP.

    O agente NUNCA deixa este caminho responder pelo cliente A2A: usamos
    `allow_input_required=True` para receber o input_required cru. Este callback
    so existiria para uma elicitation fora do fluxo de reserva, e recusa.
    """
    return ElicitResult(action="decline")


async def _chamar_reservar(
    args: dict[str, Any],
    traceparent: str | None,
    fallback_trace: str | None = None,
    input_responses: dict[str, Any] | None = None,
    request_state: str | None = None,
) -> tuple[Any, str]:
    """Fala com o servidor MCP como cliente MCP de verdade.

    Descobre por tools/list, le o resource da politica e chama reservar_sala com
    o input_required CRU (allow_input_required=True). No retry, repete o
    tools/call com id de JSON-RPC novo (cunhado pelo agente, nunca o mesmo do
    request inicial), inputResponses e o requestState ecoado sem modificacao.
    """
    meta = _trace_meta(traceparent, fallback_trace)
    async with Client(
        MCP_URL,
        client_info=CLIENT_INFO,
        elicitation_callback=_elicitation_nao_usada,
    ) as client:
        sessao = client.session
        # O dispatcher do SDK aceita o id de JSON-RPC cunhado pelo caller
        # (CallOptions["request_id"]), mas o caminho tipado nao expoe a opcao;
        # sem isto o tools/call sairia sempre com o id que a contagem da sessao
        # nova alcanca (4) e o retry repetiria o id do request inicial.
        # O decorator cobre a parte do protocolo que a sessao usa no outbound
        # (send_raw_request/notify); o resto (run, estado) delega por __getattr__,
        # o que o checador estatico nao enxerga.
        sessao._dispatcher = _DispatcherComIdsProprios(  # pyright: ignore[reportAttributeAccessIssue]
            sessao._dispatcher
        )
        descoberta = await sessao.list_tools()
        nomes = {t.name for t in descoberta.tools}
        if "reservar_sala" not in nomes:
            raise RuntimeError(f"tool reservar_sala ausente no servidor MCP: {sorted(nomes)}")
        politica = await sessao.read_resource("politica://uso")
        texto_politica = "".join(c.text for c in politica.contents if getattr(c, "text", None))
        versao = texto_politica.splitlines()[0].split(":", 1)[1].strip()
        resultado = await sessao.call_tool(
            "reservar_sala",
            args,
            input_responses=input_responses,
            request_state=request_state,
            meta=meta,
            allow_input_required=True,
        )
        return resultado, versao


def _detalhar_input_required(resultado: InputRequiredResult) -> tuple[str, list[str]]:
    """Extrai (chave, alternativas) do input_required; a ordem do enum e a ordem
    que o cliente A2A deve ver."""
    chave, pedido = next(iter(resultado.input_requests.items()))
    schema = pedido.params.requested_schema  # type: ignore[union-attr]
    campo = (schema.get("properties") or {}).get("sala", {})
    alternativas = list(campo.get("enum") or ([campo["const"]] if "const" in campo else []))
    return chave, alternativas


def _linha_alternativas(alternativas: list[str]) -> str:
    return "alternativas: " + ", ".join(alternativas)


def _texto_resultado(resultado: Any) -> str:
    return " ".join(getattr(p, "text", "") for p in (resultado.content or []))


def _aplicar_completude(task: Task, resultado: Any) -> None:
    """Traduz o resultado terminal do MCP em estado + artifact da Task."""
    dados = resultado.structured_content or {}
    if resultado.is_error:
        task.state = "TASK_STATE_FAILED"
        task.mensagem = _texto_resultado(resultado)
        return
    if dados.get("reservado") is False:
        task.state = "TASK_STATE_CANCELED"
        task.mensagem = f"Reserva recusada: {dados.get('motivo')}"
        return
    task.state = "TASK_STATE_COMPLETED"
    task.mensagem = f"Reserva {dados.get('reserva')} confirmada na {dados.get('sala')}."
    artifact = {
        "artifactId": _novo_id("art"),
        "name": "reserva",
        "parts": [
            {
                "text": json.dumps(
                    {
                        "reserva": dados.get("reserva"),
                        "sala": dados.get("sala"),
                        "inicio": dados.get("inicio"),
                        "fim": dados.get("fim"),
                        "responsavel": dados.get("responsavel"),
                        "politica": dados.get("politica"),
                    }
                )
            }
        ],
    }
    task.artifacts.append(artifact)


def _pausar(task: Task, resultado: InputRequiredResult) -> None:
    chave, alternativas = _detalhar_input_required(resultado)
    task.chave = chave
    task.alternativas = alternativas
    task.request_state = resultado.request_state
    task.state = "TASK_STATE_INPUT_REQUIRED"
    task.mensagem = _linha_alternativas(alternativas)


def _parse_pedido(texto: str) -> dict[str, Any] | None:
    partes = texto.strip().split()
    if not partes or partes[0] != "reservar":
        return None
    campos: dict[str, Any] = {}
    for token in partes[1:]:
        if "=" not in token:
            return None
        chave, valor = token.split("=", 1)
        campos[chave] = valor
    if set(campos) != {"sala", "inicio", "fim", "responsavel"}:
        return None
    return campos


def _nova_task(mensagem: dict[str, Any], traceparent: str | None) -> Task:
    task = Task(_novo_id("task"), _novo_id("ctx"))
    msg_id = mensagem.get("messageId") or _novo_id("msg")
    task.history.append(
        {"messageId": msg_id, "role": "ROLE_USER", "parts": [{"text": _texto_mensagem(mensagem)}]}
    )
    texto = _texto_mensagem(mensagem)
    args = _parse_pedido(texto)
    if args is None:
        task.state = "TASK_STATE_FAILED"
        task.mensagem = (
            "Pedido invalido. Use: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
        )
        return task
    task.args = args
    task.trace_id = (
        traceparent.strip().split("-")[1] if traceparent and len(traceparent.strip().split("-")) >= 4 else None
    )
    task.state = "TASK_STATE_WORKING"

    resultado, _ = asyncio.run(_chamar_reservar(args, traceparent))
    if isinstance(resultado, InputRequiredResult):
        _pausar(task, resultado)
    else:
        _aplicar_completude(task, resultado)
    task.add_history(task.mensagem, "ROLE_AGENT", _novo_id("msg"))
    return task


def _continuar_task(task: Task, texto: str, traceparent: str | None) -> dict[str, Any] | None:
    """Continuacao "escolha=<valor>" referenciando uma Task pausada."""
    escolha = texto.strip().removeprefix("escolha=")
    task.add_history(texto, "ROLE_USER", _novo_id("msg"))

    if escolha not in {*task.alternativas, "recusar"}:
        # Fora do enum: mantem pausada e repete a lista, byte a byte.
        task.state = "TASK_STATE_INPUT_REQUIRED"
        task.mensagem = _linha_alternativas(task.alternativas)
        task.add_history(task.mensagem, "ROLE_AGENT", _novo_id("msg"))
        return None

    assert task.args is not None and task.chave is not None
    if escolha == "recusar":
        resposta = ElicitResult(action="decline")
    else:
        resposta = ElicitResult(action="accept", content={"sala": escolha})
    resultado, _ = asyncio.run(
        _chamar_reservar(
            task.args,
            traceparent,
            fallback_trace=task.trace_id,
            input_responses={task.chave: resposta},
            request_state=task.request_state,
        )
    )
    if isinstance(resultado, InputRequiredResult):
        _pausar(task, resultado)
    else:
        _aplicar_completude(task, resultado)
    task.add_history(task.mensagem, "ROLE_AGENT", _novo_id("msg"))
    return None


def _texto_mensagem(mensagem: dict[str, Any]) -> str:
    return " ".join(p.get("text", "") for p in mensagem.get("parts") or [])


def processar(corpo: dict[str, Any], cabecalhos: dict[str, str]) -> dict[str, Any]:
    rpc_id = corpo.get("id")
    metodo = corpo.get("method")
    params = corpo.get("params") or {}
    traceparent = cabecalhos.get("traceparent")

    def erro(code: int, mensagem: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": mensagem}}

    if metodo == "GetTask":
        task_id = params.get("id")
        task = TAREFAS.get(task_id or "")
        if task is None:
            return erro(-32001, f"Task desconhecida: {task_id}")
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.dict()}}

    if metodo != "SendMessage":
        return erro(-32601, f"Metodo nao suportado: {metodo}")

    mensagem = params.get("message") or {}
    texto = _texto_mensagem(mensagem)
    task_id = mensagem.get("taskId")

    with TRAVA:
        if task_id:
            task = TAREFAS.get(task_id)
            if task is None:
                return erro(-32001, f"Task desconhecida: {task_id}")
            if task.state in ESTADOS_TERMINAIS:
                return erro(
                    -32001, f"Task {task.id} ja terminou em {task.state}; envie uma nova mensagem sem taskId"
                )
            _continuar_task(task, texto, traceparent)
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.dict()}}

        task = _nova_task(mensagem, traceparent)
        TAREFAS[task.id] = task
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.dict()}}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, corpo: dict[str, Any]) -> None:
        dados = json.dumps(corpo).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/.well-known/agent-card.json":
            self._json(200, _card())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/a2a":
            self._json(404, {"error": "not found"})
            return
        tamanho = int(self.headers.get("Content-Length", "0"))
        try:
            corpo = json.loads(self.rfile.read(tamanho) or b"{}")
        except json.JSONDecodeError:
            self._json(200, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            return
        cabecalhos = {k.lower(): v for k, v in self.headers.items()}
        self._json(200, processar(corpo, cabecalhos))

    def log_message(self, *args: Any) -> None:  # stderr limpo; o log de requests e o do MCP
        pass


def main() -> None:
    print(f"[agente] A2A em http://127.0.0.1:{PORTA} (card em /.well-known/agent-card.json)", file=sys.stderr)
    print(f"[agente] host MCP apontando para {MCP_URL}", file=sys.stderr)
    servidor = ThreadingHTTPServer(("127.0.0.1", PORTA), Handler)
    servidor.serve_forever()


if __name__ == "__main__":
    main()
