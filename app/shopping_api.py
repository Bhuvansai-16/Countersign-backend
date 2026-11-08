from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from typing import Literal

from app import auth, shopping
from app.api import cs

router = APIRouter(prefix="/api/demo/shopping")


def demo(s=Depends(cs)):
    shopping.enabled(s)
    return s


def visitor(request: Request, response: Response, s=Depends(demo)) -> str:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != s.settings.frontend_origin.rstrip("/"):
        raise HTTPException(403, "shopping requests must come from the app")
    token = request.cookies.get("cs_shopping", "")
    owner = auth.shopping_identity(s.settings.session_secret, token)
    if owner is None:
        token = auth.make_shopping_token(s.settings.session_secret)
        owner = auth.shopping_identity(s.settings.session_secret, token)
        secure = s.settings.frontend_origin.startswith("https://")
        response.set_cookie("cs_shopping", token, max_age=86400, httponly=True, secure=secure,
                            samesite="none" if secure else "lax", path="/api/demo/shopping")
    return owner


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    max_amount: str


class PrepareIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    max_amount: str
    attributes: dict[str, str] = Field(default_factory=dict)


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class ChatIn(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=12)
    max_amount: str
    attributes: dict[str, str] = Field(default_factory=dict)
    candidate_ids: list[str] = Field(default_factory=list, max_length=6)


@router.get("/status")
async def status(s=Depends(demo), owner=Depends(visitor)):
    return {"channel3": bool(s.settings.channel3_api_key), "kernel": bool(s.settings.kernel_api_key), "sandbox": True}


@router.post("/chat")
async def chat(body: ChatIn, s=Depends(demo), owner=Depends(visitor)):
    return await shopping.chat(s, owner, [x.model_dump() for x in body.messages], body.max_amount,
                               body.attributes, candidate_ids=body.candidate_ids)


@router.post("/search")
async def search(body: SearchIn, s=Depends(demo), owner=Depends(visitor)):
    return await shopping.search(s, owner, body.query, body.max_amount)


@router.post("/{candidate_id}/browser")
async def browser(candidate_id: str, s=Depends(demo), owner=Depends(visitor)):
    return await shopping.start_browser(s, owner, candidate_id)


@router.post("/{candidate_id}/inspect")
async def inspect(candidate_id: str, s=Depends(demo), owner=Depends(visitor)):
    return await shopping.inspect(s, owner, candidate_id)


@router.delete("/{candidate_id}/browser")
async def stop_browser(candidate_id: str, s=Depends(demo), owner=Depends(visitor)):
    return await shopping.stop_browser(s, owner, candidate_id)


@router.post("/{candidate_id}/mandate")
async def prepare(candidate_id: str, body: PrepareIn, s=Depends(demo), owner=Depends(visitor)):
    return await shopping.prepare(s, owner, candidate_id, body.instruction, body.max_amount, body.attributes)
