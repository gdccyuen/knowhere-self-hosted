#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH_FILE="${REPO_ROOT}/patches/pdf_service.patch"
BASE_IMAGE_TAG="${KNOWHERE_BASE_TAG:-v0.1.6}"
BASE_IMAGE="ghcr.io/ontos-ai/knowhere:${BASE_IMAGE_TAG}"
UPSTREAM_PATH_IN_IMAGE="/opt/knowhere/source/api/apps/worker/app/services/document_parser/providers/mineru/pdf_service.py"
REL_PATH_IN_REPO="apps/worker/app/services/document_parser/providers/mineru/pdf_service.py"

EDITOR="${EDITOR:-vi}"

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

work_root="${tmpdir}/work"
mkdir -p "${work_root}/$(dirname "${REL_PATH_IN_REPO}")"

echo "Extracting upstream pdf_service.py from ${BASE_IMAGE}" >&2
docker run --rm --entrypoint sh "${BASE_IMAGE}" -c "cat '${UPSTREAM_PATH_IN_IMAGE}'" \
    > "${work_root}/${REL_PATH_IN_REPO}"

if [ -f "${PATCH_FILE}" ]; then
    echo "Applying current patch" >&2
    (cd "${work_root}" && patch -p1 < "${PATCH_FILE}")
fi

echo "Opening editor (${EDITOR}) on the patched file" >&2
echo "Edit, save, quit. The patch will be regenerated automatically." >&2
"${EDITOR}" "${work_root}/${REL_PATH_IN_REPO}"

upstream_copy="${tmpdir}/upstream.pdf_service.py"
docker run --rm --entrypoint sh "${BASE_IMAGE}" -c "cat '${UPSTREAM_PATH_IN_IMAGE}'" > "${upstream_copy}"

echo "Regenerating ${PATCH_FILE}" >&2
diff -u \
    --label "a/${REL_PATH_IN_REPO}" \
    --label "b/${REL_PATH_IN_REPO}" \
    "${upstream_copy}" \
    "${work_root}/${REL_PATH_IN_REPO}" > "${PATCH_FILE}" || true

if [ ! -s "${PATCH_FILE}" ]; then
    echo "WARNING: regenerated patch is empty (no differences from upstream)" >&2
    rm -f "${PATCH_FILE}"
    exit 0
fi

echo "Patch regenerated at ${PATCH_FILE}" >&2
echo "Verifying patch applies cleanly..." >&2
verify_root="$(mktemp -d)"
trap 'rm -rf "${tmpdir}" "${verify_root}"' EXIT
mkdir -p "${verify_root}/$(dirname "${REL_PATH_IN_REPO}")"
cp "${upstream_copy}" "${verify_root}/${REL_PATH_IN_REPO}"
if ! (cd "${verify_root}" && patch -p1 --dry-run < "${PATCH_FILE}"); then
    echo "ERROR: regenerated patch does not apply cleanly. Inspect ${PATCH_FILE}." >&2
    exit 1
fi

echo "OK: regenerated patch applies cleanly." >&2
