import os
import sys

import knowhere

client = knowhere.Knowhere(
    api_key=os.environ["SMOKE_API_KEY"],
    base_url=os.environ["SMOKE_API_BASE_URL"],
)
result = client.parse(file=open(os.environ["SMOKE_PDF"], "rb"), file_name="sample.pdf")
total_chunks = result.statistics.total_chunks
full_md = result.full_markdown or ""
print(f"total_chunks={total_chunks} full_markdown_len={len(full_md)}")
if total_chunks <= 0 or not full_md:
    print("E2E parse test FAILED: no chunks or empty markdown", file=sys.stderr)
    sys.exit(1)
print("E2E parse test passed")
