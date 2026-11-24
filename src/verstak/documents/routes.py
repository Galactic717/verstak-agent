from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.types import Message

from verstak.auth import Principal, require
from verstak.db import get_session
from verstak.documents.ingest import MAX_UPLOAD_BYTES, InvalidDocument, parse_document
from verstak.documents.repository import (
    Document,
    SearchHit,
    get_document,
    save_document,
    search_documents,
)

MAX_MULTIPART_BYTES = MAX_UPLOAD_BYTES + 64 * 1024
Session = Annotated[AsyncSession, Depends(get_session)]
DocumentReader = Annotated[Principal, Depends(require("documents:read"))]
DocumentWriter = Annotated[Principal, Depends(require("documents:write"))]


class BoundedUploadRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        if "POST" not in (self.methods or set()):
            return handler

        async def read_bounded_upload(request: Request) -> Response:
            content_length = request.headers.get("content-length", "")
            if content_length.isascii() and content_length.isdecimal():
                if len(content_length) > 10 or int(content_length) > MAX_MULTIPART_BYTES:
                    raise HTTPException(413, "Upload exceeds the size limit")

            delivered = 0

            async def receive() -> Message:
                nonlocal delivered
                message = await request.receive()
                delivered += len(message.get("body", b""))
                if delivered > MAX_MULTIPART_BYTES:
                    raise HTTPException(413, "Upload exceeds the size limit")
                return message

            return await handler(Request(request.scope, receive))

        return read_bounded_upload


router = APIRouter(tags=["documents"], route_class=BoundedUploadRoute)


@router.post("/documents", status_code=201)
async def upload_document(
    file: Annotated[UploadFile, File()], principal: DocumentWriter, session: Session
) -> Document:
    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Documents may contain at most 5 MiB")
    try:
        document = await run_in_threadpool(parse_document, file.filename or "", payload)
    except InvalidDocument as exc:
        raise HTTPException(422, str(exc)) from None
    saved = await save_document(session, principal.tenant, document)
    await session.commit()
    return saved


@router.get("/documents/{document_id}")
async def read_document(document_id: UUID, principal: DocumentReader, session: Session) -> Document:
    document = await get_document(session, principal.tenant, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    return document


@router.get("/knowledge/search")
async def search_knowledge(
    q: Annotated[str, Query(min_length=1, max_length=2_000)],
    principal: DocumentReader,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> list[SearchHit]:
    return await search_documents(session, principal.tenant, q, limit)
