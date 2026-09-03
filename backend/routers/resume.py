import io
import logging

import pdfplumber
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse

log = logging.getLogger("interview")

router = APIRouter()

_PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}
_RESUME_CHAR_LIMIT = 4000


def _validate_pdf(file: UploadFile) -> None:
    name_ok = (file.filename or "").lower().endswith(".pdf")
    mime_ok = (file.content_type or "") in _PDF_MIME_TYPES
    if not name_ok or not mime_ok:
        raise HTTPException(
            status_code=400,
            detail=f"Only PDF files are accepted. Received: filename={file.filename!r}, content_type={file.content_type!r}",
        )


@router.post("/upload-resume")
async def upload_resume(file: UploadFile = File(...)) -> JSONResponse:
    _validate_pdf(file)

    raw_bytes = await file.read()
    log.info("Resume upload received: %s (%d bytes)", file.filename, len(raw_bytes))

    try:
        text_parts: list[str] = []
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
                if sum(len(t) for t in text_parts) >= _RESUME_CHAR_LIMIT:
                    break
        resume_text = "\n".join(text_parts).strip()
    except Exception as exc:
        log.exception("pdfplumber extraction failed for %s: %s", file.filename, exc)
        raise HTTPException(status_code=500, detail="Failed to process the PDF. Please try a different file.") from exc

    if not resume_text:
        raise HTTPException(
            status_code=422,
            detail="No extractable text found in the PDF. Scanned or image-only PDFs are not supported.",
        )

    resume_text = resume_text[:_RESUME_CHAR_LIMIT]
    log.info("Extracted %d chars from resume %s", len(resume_text), file.filename)

    return JSONResponse({"resume_text": resume_text})
