#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH_FILE="${REPO_ROOT}/patches/pdf_service.patch"
BASE_IMAGE_TAG="${KNOWHERE_BASE_TAG:-v0.1.6}"
BASE_IMAGE="ghcr.io/ontos-ai/knowhere:${BASE_IMAGE_TAG}"
UPSTREAM_PATH_IN_IMAGE="/opt/knowhere/source/api/apps/worker/app/services/document_parser/providers/mineru/pdf_service.py"
REL_PATH_IN_REPO="apps/worker/app/services/document_parser/providers/mineru/pdf_service.py"

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

upstream_file="${tmpdir}/upstream.pdf_service.py"

echo "Extracting upstream pdf_service.py from ${BASE_IMAGE}" >&2
docker run --rm --entrypoint sh "${BASE_IMAGE}" -c "cat '${UPSTREAM_PATH_IN_IMAGE}'" > "${upstream_file}"

work_root="${tmpdir}/work"
mkdir -p "${work_root}/$(dirname "${REL_PATH_IN_REPO}")"
cp "${upstream_file}" "${work_root}/${REL_PATH_IN_REPO}"

echo "Checking patch applies cleanly against upstream" >&2
if ! (cd "${work_root}" && patch -p1 --dry-run < "${PATCH_FILE}"); then
    echo "DRIFT DETECTED: patches/pdf_service.patch no longer applies cleanly against ${BASE_IMAGE}" >&2
    echo "Upstream pdf_service.py has changed. Regenerate the patch with scripts/edit-patch.sh." >&2
    exit 1
fi

echo "OK: patch applies cleanly against ${BASE_IMAGE}" >&2
