#!/usr/bin/env python3
"""Servidor MCP da Central de Salas (Hill Valley Tech).

Desafio "A Ponte" - servidor MCP em Streamable HTTP alinhado a revisao
2026-07-28 da spec:

- Tres tools (listar_salas, consultar_disponibilidade, reservar_sala) com
  structuredContent + bloco de texto;
- Um resource (politica://uso);
- Os dois tipos de erro (execucao com isError; protocolo como error JSON-RPC);
- O ciclo completo de MRTR (input_required) na reserva com elicitation em form
  mode, capability negociada por request e requestState protegido por
  integridade (AES-256-GCM do SDK, chave vinda de REQUEST_STATE_SECRET).

O servidor NAO guarda estado entre o input_required e o retry: tudo que ele
precisa para reconstruir o pedido viaja no requestState selado.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import (
    CancelledElicitation,
    Context,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, create_model

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"

SALAS: list[dict[str, Any]] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
RESERVAS: list[dict[str, Any]] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
POLITICA_TEXTO: str = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO: str = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()

_salas_por_id = {s["id"]: s for s in SALAS}

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class EscolhaSala(BaseModel):
    sala: str = Field(description="Sala alternativa escolhida")


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _minutos(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _conflitos(sala: str, inicio: datetime, fim: datetime) -> list[dict[str, Any]]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala and _parse(r["inicio"]) < fim and inicio < _parse(r["fim"])
    ]


def _validar_pedido(sala: str, inicio: str, fim: str) -> None:
    """Valida sala e politica. Erros de execucao com as mensagens exatas do enunciado."""
    if sala not in _salas_por_id:
        raise ToolError(f"Sala inexistente: {sala}")
    try:
        i, f = _parse(inicio), _parse(fim)
    except ValueError as exc:
        raise ToolError(ERRO_INTERVALO) from exc
    if f <= i:
        raise ToolError(ERRO_INTERVALO)
    if i.date() != f.date() or _minutos(i) < 8 * 60 or _minutos(f) > 20 * 60:
        raise ToolError(ERRO_JANELA)
    if f - i > timedelta(hours=2):
        raise ToolError(ERRO_DURACAO)


def _alternativas(sala: str, inicio: datetime, fim: datetime) -> list[str]:
    """Salas livres no intervalo com capacidade >= a da sala pedida, no maximo 3,
    ordenadas por capacidade crescente e, em empate, por id alfabetico."""
    capacidade = _salas_por_id[sala]["capacidade"]
    candidatas = [
        s["id"]
        for s in SALAS
        if s["id"] != sala
        and s["capacidade"] >= capacidade
        and not _conflitos(s["id"], inicio, fim)
    ]
    candidatas.sort(key=lambda sid: (_salas_por_id[sid]["capacidade"], sid))
    return candidatas[:3]


def _modelo_alternativas(alternativas: list[str]) -> type[EscolhaSala]:
    """Modelo pydantic cujo campo `sala` restringe a escolha as alternativas (enum)."""
    from typing import Literal

    return create_model(
        "EscolhaSalaAlternativa",
        __base__=EscolhaSala,
        sala=(
            Literal[tuple(alternativas)],  # noqa: PYI055 - tupla em Literal == valores soltos (PEP 586)
            Field(description="Sala alternativa escolhida"),
        ),
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[EscolhaSala] | EscolhaSala:
    """Resolver do MRTR: decide se precisa perguntar a alternativa.

    - Pedido invalido -> ToolError (erro de execucao, sem elicitation);
    - Sala livre -> devolve a propria sala (nada a perguntar);
    - Sala ocupada com alternativas -> Elicit (input_required);
    - Ocupada sem alternativas -> ToolError com a mensagem exata.

    O resolver roda de novo a cada rodada e RECALCULA a partir dos argumentos; o
    outcome da rodada anterior chega pelo requestState selado e e consultado pelo
    framework quando o resolver volta a perguntar.
    """
    _validar_pedido(sala, inicio, fim)
    i, f = _parse(inicio), _parse(fim)
    if not _conflitos(sala, i, f):
        return EscolhaSala(sala=sala)
    alternativas = _alternativas(sala, i, f)
    if not alternativas:
        raise ToolError(ERRO_SEM_ALTERNATIVA)
    return Elicit(
        "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
        _modelo_alternativas(alternativas),
    )


class RegistroStderr:
    """Registra cada request recebido no stderr: metodo, id e traceparent."""

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        meta = None
        if isinstance(ctx.params, Mapping):
            meta = ctx.params.get("_meta")
        traceparent = meta.get("traceparent") if isinstance(meta, Mapping) else None
        print(
            f"[central-de-salas] request method={ctx.method} id={ctx.request_id} "
            f"traceparent={traceparent}",
            file=sys.stderr,
            flush=True,
        )
        return await call_next(ctx)


def _segredo() -> bytes:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere um com:\n"
            '  export REQUEST_STATE_SECRET="$(python3 -c \'import secrets; '
            "print(secrets.token_hex(32))')\""
        )
    return segredo.encode()


servidor = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[_segredo()], ttl=600.0),
    middleware=[RegistroStderr()],
)


@servidor.tool(name="listar_salas")
def listar_salas() -> ListaDeSalas:
    """Lista todas as salas com capacidade e recursos."""
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])


@servidor.tool(name="consultar_disponibilidade")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    """Diz se uma sala esta livre no intervalo, e quais reservas conflitam."""
    _validar_pedido(sala, inicio, fim)
    conflitos = _conflitos(sala, _parse(inicio), _parse(fim))
    return Disponibilidade(
        sala=sala, livre=not conflitos, conflitos=[ConflitoOut(**c) for c in conflitos]
    )


@servidor.tool(name="reservar_sala")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[EscolhaSala], Resolve(escolha_de_sala)],
) -> ReservaOut:
    """Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar."""
    if isinstance(escolha, (DeclinedElicitation, CancelledElicitation)):
        return ReservaOut(reservado=False, motivo="recusado")
    alvo = escolha.data.sala
    i, f = _parse(inicio), _parse(fim)
    if _conflitos(alvo, i, f):
        # Defensivo: o caminho de retry chega com o pedido original selado; nao ha
        # corrida dentro da mesma execucao (estado de reservas e do processo).
        raise ToolError(ERRO_SEM_ALTERNATIVA)
    numero = max(
        (int(r["id"].split("-")[1]) for r in RESERVAS if r["id"].startswith("res-")),
        default=0,
    ) + 1
    nova = {
        "id": f"res-{numero:04d}",
        "sala": alvo,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
    }
    RESERVAS.append(nova)
    return ReservaOut(
        reserva=nova["id"],
        reservado=True,
        sala=alvo,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=POLITICA_VERSAO,
        motivo=None,
    )


@servidor.resource("politica://uso", mime_type="text/markdown")
def politica_de_uso() -> str:
    """Politica de uso das salas (versao no primeiro line)."""
    return POLITICA_TEXTO


def main() -> None:
    import uvicorn

    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = servidor.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host="127.0.0.1",
    )
    uvicorn.run(app, host="127.0.0.1", port=porta)


if __name__ == "__main__":
    main()
