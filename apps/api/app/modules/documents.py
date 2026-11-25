"""Project document uploads (INPROOM-165 / story INPROOM-28).

An uploaded document is a source item of kind `upload`: source_id is the project itself (its uploads
"source"), external_id is the document id. Replacing it appends a revision; deleting it marks the item DELETED
(lineage kept, retention purges it later). Parsing and every other derivative follow from INPROOM-27.

The media type is decided from the bytes, never from the client: PDF and DOCX by their signatures; Markdown,
HTML and text by file extension, and only if the bytes are UTF-8 text. Anything else gets 415.
"""

import io
import re
import unicodedata
import uuid
import zipfile
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Header, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import Connection, select

from app import audit
from app.api_errors import ApiError, conflict, precondition_failed
from app.auth import CurrentPrincipal
from app.authz import authorize
from app.config import get_settings
from app.content import ingest
from app.content import store as blobs
from app.content.revisions import record_revision
from app.db import tenant_session
from app.db_schema import source_objects
from app.idempotency import request_fingerprint, run_idempotent
from app.modules import settings as settings_module

router = APIRouter(tags=["documents"])
PROBLEM = {"description": "problem+json"}
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".txt": "text/plain",
    ".text": "text/plain",
    "": "text/plain",
}
SUPPORTED = "PDF, DOCX, Markdown (.md), HTML (.html) or plain text (.txt)"


class UploadedDocument(BaseModel):
    document_id: str
    title: str
    filename: str
    media_type: str
    size_bytes: int
    content_sha256: str
    seq: int
    created: bool
    status: str
    first_seen_at: datetime | None = None


def clean_filename(name: str | None) -> str:
    """Basename only, no control characters, at most 200 characters; never empty."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in unicodedata.normalize("NFC", name) if unicodedata.category(c)[0] != "C")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:200] or "document"


def sniff(data: bytes, filename: str) -> str:
    """The media type, from the bytes (signatures) and, for text, the extension. 415 when unsupported."""
    if data.startswith(b"%PDF-"):
        return PDF
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" in z.namelist():
                    return DOCX
        except zipfile.BadZipFile:
            pass
        raise _unsupported("a zip archive that is not a Word document")
    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[1].lower()
    if ext not in TEXT_TYPES:
        raise _unsupported(f"files of type {ext}")
    if b"\x00" in data:
        raise _unsupported("binary content")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _unsupported("text that is not UTF-8") from exc
    return TEXT_TYPES[ext]


def _unsupported(what: str) -> ApiError:
    return ApiError(
        415, "UNSUPPORTED_MEDIA_TYPE", "Unsupported document", f"Cannot accept {what}; use {SUPPORTED}"
    )


async def _read(file: UploadFile) -> bytes:
    limit = get_settings().content_max_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ApiError(413, "DOCUMENT_TOO_LARGE", "Document too large", f"The limit is {limit} bytes")
    if not data:
        raise ApiError(422, "EMPTY_UPLOAD", "Empty upload", "The file has no content")
    return data


def _writable(conn: Connection, tenant: uuid.UUID, project: dict[str, Any]) -> None:
    if project["status"] == "ARCHIVED":
        raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
    allowed = settings_module.current_settings(conn, tenant, project).settings.sources.allowed_kinds
    if "upload" not in allowed:
        raise conflict("UPLOADS_NOT_ALLOWED", "This project's settings do not allow uploaded documents")


def _item(conn: Connection, project_id: uuid.UUID, document_id: str) -> dict[str, Any]:
    row = (
        conn.execute(
            select(source_objects).where(
                source_objects.c.project_id == project_id,
                source_objects.c.source_kind == "upload",
                source_objects.c.source_id == project_id,
                source_objects.c.external_id == document_id,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(404, "DOCUMENT_NOT_FOUND", "Not found", "No such document in this project")
    return dict(row)


def _store(
    conn: Connection,
    principal,
    project_id: uuid.UUID,
    document_id: str,
    data: bytes,
    media_type: str,
    title: str,
    filename: str,
) -> UploadedDocument:
    done = record_revision(
        conn,
        blobs.get_store(),
        tenant=principal.tenant_id,
        project_id=project_id,
        source_kind="upload",
        source_id=project_id,
        external_id=document_id,
        data=data,
        media_type=media_type,
        title=title,
        uri=None,
        acl={"visibility": "project", "uploaded_by": principal.user_id, "filename": filename},
    )
    return UploadedDocument(
        document_id=document_id,
        title=title,
        filename=filename,
        media_type=media_type,
        size_bytes=len(data),
        content_sha256=done.content_sha256,
        seq=done.seq,
        created=done.created,
        status=done.status,
    )


def _audit(
    conn, principal, request: Request, action: str, project_id, document_id: str, payload: dict
) -> None:
    audit.record(
        conn,
        principal,
        action,
        "document",
        document_id,
        trace_id=getattr(request.state, "trace_id", None),
        payload=payload,
        project_id=project_id,
    )


@router.post(
    "/v1/projects/{project_id}/documents",
    response_model=UploadedDocument,
    status_code=201,
    summary="Upload a document (PDF, DOCX, Markdown, HTML, text); it is stored, versioned and parsed",
    responses={404: PROBLEM, 409: PROBLEM, 413: PROBLEM, 415: PROBLEM, 422: PROBLEM},
)
async def upload(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="The document")],
    title: Annotated[str | None, Form(max_length=300)] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    data = await _read(file)
    filename = clean_filename(file.filename)
    media_type = sniff(data, filename)
    name = (title or "").strip() or filename
    document_id = str(uuid.uuid4())
    body = {"sha256": blobs.sha256(data), "filename": filename, "title": name}

    def handler(conn: Connection) -> tuple[int, dict[str, Any]]:
        access = authorize(conn, principal, request, "project.content.upload", project_id, lock=True)
        _writable(conn, principal.tenant_id, access.project)
        result = _store(conn, principal, project_id, document_id, data, media_type, name, filename)
        _audit(
            conn, principal, request, "content.upload", project_id, document_id, body | {"type": media_type}
        )
        return 201, result.model_dump(mode="json")

    fingerprint = request_fingerprint("POST", f"/v1/projects/{project_id}/documents", body)
    return run_idempotent(principal.tenant_id, idempotency_key, fingerprint, handler)


@router.put(
    "/v1/projects/{project_id}/documents/{document_id}",
    response_model=UploadedDocument,
    summary="Replace a document's content (If-Match: the current revision seq); identical bytes add nothing",
    responses={404: PROBLEM, 409: PROBLEM, 412: PROBLEM, 413: PROBLEM, 415: PROBLEM, 428: PROBLEM},
)
async def replace(
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="The new content")],
    title: Annotated[str | None, Form(max_length=300)] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> UploadedDocument:
    data = await _read(file)
    filename = clean_filename(file.filename)
    media_type = sniff(data, filename)
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.content.upload", project_id, lock=True)
        _writable(conn, principal.tenant_id, access.project)
        item = _item(conn, project_id, str(document_id))
        if item["status"] == "DELETED":
            raise ApiError(404, "DOCUMENT_NOT_FOUND", "Not found", "The document was deleted")
        if if_match is None:
            raise ApiError(
                428, "PRECONDITION_REQUIRED", "If-Match required", "Send If-Match with the current seq"
            )
        if if_match.strip('"') != str(item["revisions"]):
            raise precondition_failed(f"The document is at revision {item['revisions']}")
        name = (title or "").strip() or item["title"]
        result = _store(conn, principal, project_id, str(document_id), data, media_type, name, filename)
        _audit(
            conn,
            principal,
            request,
            "content.replace",
            project_id,
            str(document_id),
            {"sha256": result.content_sha256, "filename": filename, "seq": result.seq},
        )
    response.headers["ETag"] = f'"{result.seq}"'
    return result


@router.delete(
    "/v1/projects/{project_id}/documents/{document_id}",
    status_code=204,
    summary="Delete a document: its content is withheld at once and purged by the retention policy",
    responses={404: PROBLEM, 409: PROBLEM},
)
def remove(
    request: Request, principal: CurrentPrincipal, project_id: uuid.UUID, document_id: uuid.UUID
) -> Response:
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.content.upload", project_id, lock=True)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        item = _item(conn, project_id, str(document_id))
        if item["status"] != "DELETED":  # idempotent: deleting twice changes nothing
            ingest.mark(
                conn, project_id, "upload", project_id, [str(document_id)], "DELETED", "deleted by a user"
            )
            _audit(
                conn,
                principal,
                request,
                "content.delete",
                project_id,
                str(document_id),
                {"title": item["title"]},
            )
    return Response(status_code=204)
