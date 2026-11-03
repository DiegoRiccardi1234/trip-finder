"""Authenticated browser endpoints and capability-only dispatch for local MCP."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app import agent_security as security
from app.agent_service import AgentService, AgentToolError

require_ui = security.require_ui


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolRequest(Input):
    arguments: dict[str, Any] = Field(default_factory=dict)


class SearchInput(Input):
    plan: dict[str, Any]
    request_id: str = Field(min_length=1, max_length=128)


class ConversationInput(Input):
    title: str = Field(default="Nuovo viaggio", min_length=1, max_length=100)
    context: dict[str, Any] | None = None
    operation_id: str | None = None
    itinerary_id: str | None = None


class MessageInput(Input):
    text: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=128)
    max_calls: int = Field(default=6, ge=1, le=6, strict=True)


class SaveInput(Input):
    operation_id: str
    itinerary_id: str
    name: str = Field(min_length=1, max_length=80)
    request_id: str = Field(min_length=1, max_length=128)


class ProfileInput(Input):
    kind: str
    payload: dict[str, Any]
    request_id: str = Field(min_length=1, max_length=128)


async def _call(awaitable):
    try:
        return await awaitable
    except AgentToolError as exc:
        raise HTTPException(exc.status_code, exc.code) from None


def build_router(service: AgentService) -> APIRouter:
    router = APIRouter(prefix="/api/agent", tags=["local assistants"])
    protected = APIRouter(dependencies=[Depends(require_ui)])

    @router.post("/ui/session")
    async def ui_session(request: Request, response: Response):
        security.require_local(request, origin_required=True)
        if request.headers.get("x-tripfinder-ui") != "1":
            raise HTTPException(403, "ui_header_required")
        session, csrf = security.new_ui_session()
        response.set_cookie(security.COOKIE, session, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=12*3600, path="/")
        response.headers["Cache-Control"] = "no-store"
        return {"csrf_token": csrf}

    @protected.get("/status")
    async def status():
        return {"enabled": True, "endpoint": "/mcp/", "pending_proposals": sum(p["status"] == "pending" for p in await service.list_proposals())}

    @protected.post("/setup")
    async def setup(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            return security.setup(f"{request.url.scheme}://{request.url.netloc}")
        except (ValueError, OSError):
            raise HTTPException(503, "mcp_setup_failed") from None

    @router.post("/tools/{name}")
    async def tool(request: Request, name: str, payload: ToolRequest):
        security.require_mcp(request)
        return await _call(service.dispatch(name, payload.arguments))

    @protected.get("/proposals")
    async def proposals():
        return {"proposals": await service.list_proposals()}

    @protected.post("/proposals/{proposal_id}/confirm")
    async def confirm(proposal_id: str):
        return await _call(service.decide_proposal(proposal_id, True))

    @protected.post("/proposals/{proposal_id}/reject")
    async def reject(proposal_id: str):
        return await _call(service.decide_proposal(proposal_id, False))

    @protected.post("/proposals/save-itinerary")
    async def propose_save(payload: SaveInput):
        return await _call(service.propose_save_itinerary(**payload.model_dump()))

    @protected.post("/proposals/profile")
    async def propose_profile(payload: ProfileInput):
        return await _call(service.propose_profile_change(**payload.model_dump()))

    @protected.get("/saved-itineraries")
    async def saved():
        return {"itineraries": await service.list_saved()}

    @protected.get("/saved-itineraries/{saved_id}")
    async def saved_one(saved_id: str):
        return await _call(service.get_saved(saved_id))

    @protected.get("/operations")
    async def operations(conversation_id: str | None = None):
        return {"operations": await _call(service.list_operations(conversation_id))}

    @protected.post("/operations")
    async def start(payload: SearchInput):
        return await _call(service.start_search(**payload.model_dump()))

    @protected.get("/operations/{operation_id}")
    async def operation(operation_id: str):
        return await _call(service.get_operation(operation_id))

    @protected.post("/operations/{operation_id}/cancel")
    async def cancel(operation_id: str):
        return await _call(service.cancel_operation(operation_id))

    @protected.get("/conversations")
    async def conversations():
        return {"conversations": await service.list_conversations()}

    @protected.post("/conversations")
    async def new_conversation(payload: ConversationInput):
        return await _call(service.create_conversation(**payload.model_dump()))

    @protected.get("/conversations/{conversation_id}")
    async def conversation(conversation_id: str):
        return await _call(service.get_conversation(conversation_id))

    @protected.post("/conversations/{conversation_id}/messages")
    async def message(conversation_id: str, payload: MessageInput):
        return await _call(service.chat(conversation_id, **payload.model_dump()))

    @protected.post("/conversations/{conversation_id}/cancel")
    async def cancel_message(conversation_id: str):
        return await _call(service.cancel_chat(conversation_id))

    @protected.get("/connections")
    async def connections():
        from app.ai.connections import snapshot
        return await snapshot()

    @protected.post("/connections")
    async def save_connection(payload: dict):
        from app.ai.connections import save_connection as save
        try:
            return await save(payload)
        except (ValueError, TypeError):
            raise HTTPException(400, "invalid_connection") from None

    @protected.put("/routing")
    async def routing(payload: dict):
        from app.ai.connections import save_routes
        try:
            return await save_routes(payload)
        except (ValueError, TypeError):
            raise HTTPException(400, "invalid_routing") from None

    @protected.get("/runtimes/{kind}")
    async def runtime(kind: str):
        if kind not in {"codex", "claude"}:
            raise HTTPException(404, "unknown_runtime")
        from app.ai.connections import runtime_status
        return await runtime_status(kind)

    @protected.get("/connections/{connection_id}/status")
    async def connection_status(connection_id: str):
        from app.ai.connections import connection_status
        try:
            return await connection_status(connection_id)
        except ValueError:
            raise HTTPException(404, "unknown_connection") from None

    @protected.post("/connections/{connection_id}/probe")
    async def connection_probe(connection_id: str):
        from app.ai.connections import probe_connection
        try:
            return await probe_connection(connection_id)
        except ValueError:
            raise HTTPException(404, "unknown_connection") from None

    router.include_router(protected)
    return router
