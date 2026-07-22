import fnmatch
import io
import os
import pathlib
import zipfile
from typing import Optional

import requests
from app.services.document_parser.providers.mineru.client import (
    get_mineru_headers,
    get_mineru_session,
    mineru_logger,
    raise_mineru_unavailable,
)
from app.services.document_parser.providers.mineru.quota_manager import get_mineru_quota_manager
from app.services.document_parser.providers.mineru.task_polling import (
    get_batch_status,
    poll_mineru_task,
)
from app.services.document_parser.support.parser_log_utils import truncate_log_value

from shared.core.config import settings
from shared.core.constants import APIConstants
from shared.core.exceptions.domain_exceptions import (
    MinerUServiceException,
    StorageServiceException,
    UnavailableException,
)
from shared.services.storage.job_file_storage import JobFileStorage
from app.services.common.file_loading import is_remote

MINERU_UPLOAD_TIMEOUT = (
    settings.MINERU_UPLOAD_CONNECT_TIMEOUT,
    settings.MINERU_UPLOAD_READ_TIMEOUT,
)

_MINERU_LOCAL_MODE = os.environ.get("MINERU_LOCAL_MODE", "false").lower() == "true"
_MINERU_LOCAL_TIMEOUT = int(os.environ.get("MINERU_LOCAL_TIMEOUT", "600"))


def _should_use_mineru_s3_url_mode(s3_key: Optional[str]) -> bool:
    return not settings.MINERU_UPLOAD_MODE_ENABLED and s3_key is not None


def _log_mineru_url_mode_storage_fallback(
    operation: str,
    s3_key: str,
    local_file_path: Optional[str],
    exc: Exception,
) -> None:
    mineru_logger(
        "url_mode_storage_fallback",
        operation=operation,
        source_s3_key=s3_key,
        local_file_path=local_file_path,
        error_type=type(exc).__name__,
        error_message=truncate_log_value(exc),
    ).warning(
        "MinerU URL-mode storage preparation failed. Falling back to direct upload."
    )


def _log_mineru_url_mode_ingestion_fallback(
    operation: str,
    s3_key: str,
    pdf_url: str,
    exc: Exception,
) -> None:
    mineru_logger(
        "url_mode_ingestion_fallback",
        operation=operation,
        source_s3_key=s3_key,
        source_kind="remote_url" if is_remote(pdf_url) else "local_file",
        source_path=None if is_remote(pdf_url) else pdf_url,
        error_type=type(exc).__name__,
        error_message=truncate_log_value(exc),
    ).warning("MinerU URL-mode ingestion setup failed. Falling back to direct upload.")


def _inspect_mineru_source_s3_key(s3_key: Optional[str]) -> tuple[Optional[str], bool]:
    if not _should_use_mineru_s3_url_mode(s3_key):
        return None, False

    assert s3_key is not None
    try:
        existing_file = JobFileStorage().verify_upload_exists(s3_key)
    except Exception as exc:
        _log_mineru_url_mode_storage_fallback(
            operation="verify_source_object",
            s3_key=s3_key,
            local_file_path=None,
            exc=exc,
        )
        return None, False

    if existing_file.get("exists"):
        mineru_logger(
            "url_mode_source_reused",
            source_s3_key=s3_key,
        ).info("Reusing existing S3 source for MinerU URL mode")
        return s3_key, True

    return None, True


def get_existing_mineru_source_s3_key(s3_key: Optional[str]) -> Optional[str]:
    existing_s3_key, _ = _inspect_mineru_source_s3_key(s3_key)
    return existing_s3_key


def resolve_mineru_source_s3_key(
    s3_key: Optional[str],
    local_file_path: Optional[str] = None,
) -> Optional[str]:
    existing_s3_key, can_prepare_url_mode = _inspect_mineru_source_s3_key(s3_key)
    if existing_s3_key is not None:
        return existing_s3_key

    if not can_prepare_url_mode:
        return None

    if local_file_path is None or is_remote(local_file_path):
        return None

    assert s3_key is not None
    try:
        JobFileStorage().upload_source_file(local_file_path, s3_key)
    except Exception as exc:
        _log_mineru_url_mode_storage_fallback(
            operation="upload_source_object",
            s3_key=s3_key,
            local_file_path=local_file_path,
            exc=exc,
        )
        return None

    mineru_logger(
        "url_mode_source_uploaded",
        source_s3_key=s3_key,
        local_file_path=local_file_path,
    ).info("Uploaded local PDF to S3 for MinerU URL mode")
    return s3_key


def _flatten_extracted_zip(output_dir: str, keep_exts: tuple[str, ...], exclude_patterns: tuple[str, ...]) -> None:
    dest = pathlib.Path(output_dir)
    for extracted_path in dest.rglob("*"):
        if not extracted_path.is_file():
            continue
        should_exclude = any(
            p in extracted_path.name or fnmatch.fnmatch(extracted_path.name, p)
            for p in exclude_patterns
        )
        if should_exclude:
            extracted_path.unlink()
            continue
        if extracted_path.suffix.lower() not in keep_exts:
            extracted_path.unlink()
            continue
        target = dest / extracted_path.relative_to(dest)
        if extracted_path != target:
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted_path.rename(target)
    for directory in sorted(
        [p for p in dest.rglob("*") if p.is_dir()],
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            next(directory.iterdir())
        except StopIteration:
            directory.rmdir()


def parse_via_local(pdf_path: str, filename: str, output_dir: str) -> None:
    local_logger = mineru_logger(
        "local_parse",
        operation="local_file_parse",
        filename=filename,
    )

    url = f"{settings.MINERU_URL}/file_parse"

    if is_remote(pdf_path):
        raise MinerUServiceException(
            internal_message="Local MinerU mode does not support remote URLs; "
            "pdf_path must be a local file",
        )

    local_logger.info("Posting PDF to local MinerU /file_parse")
    with open(pdf_path, "rb") as f:
        response = get_mineru_session().post(
            url,
            files={"files": (filename, f)},
            data={
                "lang_list": "ch",
                "backend": "pipeline",
                "parse_method": "auto",
                "return_md": True,
                "return_images": True,
                "return_content_list": True,
                "response_format_zip": True,
            },
            timeout=_MINERU_LOCAL_TIMEOUT,
        )

    if response.status_code != 200:
        local_logger.bind(status_code=response.status_code).error(
            "Local MinerU /file_parse failed"
        )
        raise MinerUServiceException(
            internal_message=f"Local MinerU /file_parse failed: {response.text}",
            status_code=response.status_code,
        )

    content_type = response.headers.get("content-type", "")

    if "zip" in content_type or response.content[:4] == b"PK\x03\x04":
        local_logger.info("Received ZIP from local MinerU, extracting")
        os.makedirs(output_dir, exist_ok=True)
        zip_bytes = io.BytesIO(response.content)
        keep_exts = (".md", ".jpg", ".jpeg", ".png", ".gif", ".json")
        exclude_patterns = ("content_list", "middle.json", "model.json")
        with zipfile.ZipFile(zip_bytes, "r") as zf:
            zf.extractall(output_dir)
        _flatten_extracted_zip(output_dir, keep_exts, exclude_patterns)
        md_src = None
        for md_file in pathlib.Path(output_dir).rglob("*.md"):
            md_src = md_file
            break
        if md_src is not None and md_src.name != "full.md":
            full_md_path = pathlib.Path(output_dir) / "full.md"
            md_src.rename(full_md_path)
        local_logger.info("Local MinerU ZIP extraction complete")
    else:
        local_logger.info("Received JSON from local MinerU, writing full.md")
        result = response.json()
        if result.get("code") is not None and result.get("code") != 0:
            raise MinerUServiceException(
                internal_message=f"Local MinerU API error: {result.get('msg', 'Unknown')}"
            )
        md_content = ""
        results = result.get("results", {})
        for _key, val in results.items():
            md_content = val.get("md_content", "")
            break
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, "full.md"), "w", encoding="utf-8") as mf:
            mf.write(md_content)
        local_logger.info("Local MinerU JSON extraction complete")


def _request_upload_target(pdf_url: str, filename: str) -> tuple[str, str, str]:
    base_url = settings.MINERU_URL
    quota_manager = get_mineru_quota_manager()
    upload_logger = mineru_logger(
        "upload_url",
        operation="upload_url",
        filename=filename,
        source_kind="remote_url" if is_remote(pdf_url) else "local_file",
    )
    url = f"{base_url}/file-urls/batch"
    payload = {
        "files": [
            {
                "name": filename,
                "is_ocr": True,
            }
        ],
        "enable_formula": True,
        "enable_table": True,
        "language": "auto",
        "model_version": "vlm",
    }

    upload_logger.info("Requesting MinerU upload URL")
    lease = quota_manager.acquire_request(operation="upload_url")
    upload_logger.bind(token_id=lease.token_id).info(
        "Acquired MinerU token for upload URL"
    )
    response = get_mineru_session().post(
        url,
        headers=get_mineru_headers(lease.api_key),
        json=payload,
        timeout=settings.MINERU_API_TIMEOUT,
    )
    if response.status_code == 429:
        raise_mineru_unavailable(lease.token_id, response, operation="upload_url")
    if response.status_code != 200:
        upload_logger.bind(
            token_id=lease.token_id,
            status_code=response.status_code,
        ).error("Failed to get MinerU upload URL")
        raise MinerUServiceException(
            internal_message=f"Failed to get upload URL: {response.text}",
            status_code=response.status_code,
        )

    result = response.json()
    if result.get("code") != 0:
        response_message = str(result.get("msg", "Unknown error"))
        if "rate limit" in response_message.lower():
            quota_manager.mark_rate_limited(
                lease.token_id,
                settings.MINERU_TOKEN_COOLDOWN_SECONDS,
            )
            upload_logger.bind(
                token_id=lease.token_id,
                retry_after=settings.MINERU_TOKEN_COOLDOWN_SECONDS,
                error_message=response_message,
            ).warning("MinerU upload URL request hit rate limit")
            raise UnavailableException(
                internal_message=f"MinerU rate limited during upload_url: {response_message}",
                retry_after=settings.MINERU_TOKEN_COOLDOWN_SECONDS,
                limit=lease.rpm_limit,
                period="minute",
                user_message="Document processing is busy right now. Please retry shortly.",
            )
        upload_logger.bind(
            token_id=lease.token_id,
            error_message=response_message,
        ).error("MinerU upload URL request returned API error")
        raise MinerUServiceException(
            internal_message=f"MinerU API error: {response_message}"
        )

    batch_id = result["data"]["batch_id"]
    upload_url = result["data"]["file_urls"][0]
    upload_logger.bind(token_id=lease.token_id, batch_id=batch_id).info(
        "Received MinerU upload URL"
    )
    return batch_id, upload_url, lease.token_id


def _upload_file_to_mineru(
    pdf_url: str, filename: str, upload_url: str, token_id: str
) -> None:
    upload_logger = mineru_logger(
        "file_upload",
        operation="file_upload",
        filename=filename,
        token_id=token_id,
        source_kind="remote_url" if is_remote(pdf_url) else "local_file",
    )

    if is_remote(pdf_url):
        import tempfile

        upload_logger.info("Downloading remote source file before MinerU upload")
        try:
            download_response = get_mineru_session().get(
                pdf_url,
                stream=True,
                timeout=APIConstants.S3_FILE_DOWNLOAD_TIMEOUT,
            )
            download_response.raise_for_status()

            with tempfile.NamedTemporaryFile(
                delete=False, suffix=os.path.splitext(filename)[1]
            ) as temp_file:
                for chunk in download_response.iter_content(chunk_size=8192):
                    temp_file.write(chunk)
                temp_path = temp_file.name

            upload_logger.bind(temp_file_path=temp_path).info(
                "Uploading staged file to MinerU"
            )
            with open(temp_path, "rb") as file_obj:
                upload_response = get_mineru_session().put(
                    upload_url,
                    data=file_obj,
                    timeout=MINERU_UPLOAD_TIMEOUT,
                )

            os.unlink(temp_path)
        except requests.RequestException as exc:
            upload_logger.bind(error_message=str(exc)).error(
                "Failed to stage remote source file for MinerU"
            )
            raise StorageServiceException(
                internal_message=f"Failed to download remote file: {exc}"
            )
    else:
        upload_logger.bind(local_path=pdf_url).info("Uploading local file to MinerU")
        try:
            with open(pdf_url, "rb") as file_obj:
                try:
                    upload_response = get_mineru_session().put(
                        upload_url,
                        data=file_obj,
                        timeout=MINERU_UPLOAD_TIMEOUT,
                    )
                except requests.RequestException as exc:
                    upload_logger.bind(error_message=str(exc)).error(
                        "Failed to upload local file to MinerU"
                    )
                    raise MinerUServiceException(
                        internal_message=f"Failed to upload file to MinerU: {exc}",
                        original_exception=exc,
                    ) from exc
        except OSError as exc:
            upload_logger.bind(error_message=str(exc)).error(
                "Failed to read local file for MinerU upload"
            )
            raise StorageServiceException(
                internal_message=f"Failed to read local file: {exc}",
                original_exception=exc,
            ) from exc

    if upload_response.status_code != 200:
        upload_logger.bind(status_code=upload_response.status_code).error(
            "MinerU file upload failed"
        )
        raise MinerUServiceException(
            internal_message=f"Failed to upload file to MinerU: {upload_response.text}",
            status_code=upload_response.status_code,
        )

    upload_logger.info("MinerU file upload completed, switching to polling")


def _submit_url_task(presigned_url: str, filename: str) -> tuple[str, str]:
    base_url = settings.MINERU_URL
    quota_manager = get_mineru_quota_manager()
    submit_logger = mineru_logger(
        "submit_url_task",
        operation="submit_url_task",
        filename=filename,
    )

    url = f"{base_url}/extract/task/batch"
    payload = {
        "files": [{"url": presigned_url}],
        "is_ocr": True,
        "enable_formula": True,
        "enable_table": True,
        "language": "auto",
        "model_version": "vlm",
    }

    submit_logger.info("Submitting URL-based MinerU extraction task")
    lease = quota_manager.acquire_request(operation="submit_url_task")
    submit_logger.bind(token_id=lease.token_id).info(
        "Acquired MinerU token for URL task submission"
    )

    response = get_mineru_session().post(
        url,
        headers=get_mineru_headers(lease.api_key),
        json=payload,
        timeout=settings.MINERU_API_TIMEOUT,
    )

    if response.status_code == 429:
        raise_mineru_unavailable(lease.token_id, response, operation="submit_url_task")

    if response.status_code != 200:
        submit_logger.bind(
            token_id=lease.token_id,
            status_code=response.status_code,
        ).error("MinerU URL task submission failed")
        raise MinerUServiceException(
            internal_message=f"URL task submission failed: {response.text}",
            status_code=response.status_code,
        )

    result = response.json()
    if result.get("code") != 0:
        response_message = str(result.get("msg", "Unknown error"))
        if "rate limit" in response_message.lower():
            quota_manager.mark_rate_limited(
                lease.token_id,
                settings.MINERU_TOKEN_COOLDOWN_SECONDS,
            )
            raise UnavailableException(
                internal_message=f"MinerU rate limited during submit_url_task: {response_message}",
                retry_after=settings.MINERU_TOKEN_COOLDOWN_SECONDS,
                limit=lease.rpm_limit,
                period="minute",
                user_message="Document processing is busy right now. Please retry shortly.",
            )
        raise MinerUServiceException(
            internal_message=f"MinerU API error: {response_message}"
        )

    batch_id = result["data"]["batch_id"]
    submit_logger.bind(token_id=lease.token_id, batch_id=batch_id).info(
        "MinerU URL task submitted"
    )
    return batch_id, lease.token_id


def parse_via_full(
    pdf_url: str,
    filename: str,
    output_dir: str,
    s3_key: Optional[str] = None,
) -> None:
    if _MINERU_LOCAL_MODE:
        return parse_via_local(pdf_url, filename, output_dir)

    batch_id: str | None = None
    token_id: str | None = None
    resolved_s3_key = resolve_mineru_source_s3_key(
        s3_key=s3_key,
        local_file_path=None if is_remote(pdf_url) else pdf_url,
    )

    if resolved_s3_key is not None:
        try:
            presigned = JobFileStorage().generate_upload_download_url(
                resolved_s3_key, expires_in=settings.MINERU_URL_MODE_PRESIGN_EXPIRY
            )
            presigned_url = presigned["download_url"]
            mineru_logger("ingestion_mode", mode="s3_url").info(
                "Using S3 URL mode for MinerU ingestion"
            )
            batch_id, token_id = _submit_url_task(presigned_url, filename)
        except Exception as exc:
            _log_mineru_url_mode_ingestion_fallback(
                operation="start_url_mode_ingestion",
                s3_key=resolved_s3_key,
                pdf_url=pdf_url,
                exc=exc,
            )
            resolved_s3_key = None

    if resolved_s3_key is None:
        mineru_logger("ingestion_mode", mode="direct_upload").info(
            "Using direct upload mode for MinerU ingestion"
        )
        batch_id, upload_url, token_id = _request_upload_target(pdf_url, filename)
        _upload_file_to_mineru(pdf_url, filename, upload_url, token_id)

    if batch_id is None or token_id is None:
        raise MinerUServiceException(
            internal_message="MinerU task setup completed without a batch id or token"
        )

    poll_mineru_task(
        status_url=f"{settings.MINERU_URL}/extract-results/batch/{batch_id}",
        task_id=batch_id,
        output_dir=output_dir,
        get_status=get_batch_status,
        preferred_token_id=token_id,
    )
