"""HTTP routes for offline triage rule package workflow."""

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import triage_service as service
from .auth import current_user, require_roles

router = APIRouter(prefix="/api/triage-packages", tags=["离线分流规则包"])


class ReviewPointIn(BaseModel):
    review_point: str = Field(min_length=2, max_length=120)


class CheckpointIn(BaseModel):
    checkpoint: dict


class SubmissionIn(BaseModel):
    package_hash: str = Field(min_length=64, max_length=64)
    leaf_results: list
    node_checks: list
    saved_at: str | None = None


class ReturnIn(BaseModel):
    reason: str = Field(min_length=4, max_length=500)


@router.get("")
def packages(user=Depends(current_user)):
    return {"packages": service.list_packages(), "claims": service.list_claims()}


@router.post("", status_code=201)
def issue(user=Depends(require_roles("supervisor"))):
    return service.issue_package(user)


@router.get("/{package_id}")
def detail(package_id: int, user=Depends(current_user)):
    return service.package_detail(package_id)


@router.get("/{package_id}/download")
def download(package_id: int, user=Depends(current_user)):
    package = service.download_package(package_id)
    # Pretty, deterministic offline artefact; attachment semantics are handled
    # in the workbench via a blob download.
    return JSONResponse(package, headers={
        "Content-Disposition": f'attachment; filename="{package["package_no"]}.json"',
        "X-Package-Hash": package["content_hash"],
    })


@router.post("/{package_id}/claims")
def claim(package_id: int, payload: ReviewPointIn, response: Response,
          user=Depends(require_roles("operator", "supervisor"))):
    view, created = service.claim_package(package_id, payload.review_point, user)
    # A retried claim resumes the same record: 200 instead of a new 201.
    response.status_code = 201 if created else 200
    return view


@router.put("/claims/{claim_token}/checkpoint")
def checkpoint(claim_token: str, payload: CheckpointIn,
               user=Depends(require_roles("operator", "supervisor"))):
    return service.save_progress(claim_token, payload.checkpoint, user)


@router.post("/claims/{claim_token}/submit")
def submit(claim_token: str, payload: SubmissionIn,
           user=Depends(require_roles("operator", "supervisor"))):
    view, _ = service.submit_for_review(claim_token, payload.model_dump(), user)
    return view


@router.post("/claims/{claim_id}/accept")
def accept(claim_id: int, user=Depends(require_roles("supervisor"))):
    return service.accept_claim(claim_id, user)


@router.post("/claims/{claim_id}/return")
def return_claim(claim_id: int, payload: ReturnIn,
                 user=Depends(require_roles("supervisor"))):
    return service.return_claim(claim_id, payload.reason, user)
