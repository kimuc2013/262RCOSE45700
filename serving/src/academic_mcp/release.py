"""Public build identity, independent of cached answers or runtime secrets."""
import os
from pathlib import Path
import re
import uuid

BUILD_FILE=Path('/app/.serving-release')
INSTANCE=uuid.uuid4().hex[:12]


def release_metrics():
    # The image's baked identity wins over environment configuration so that
    # rollback cannot label an older image as the requested new commit.
    raw=BUILD_FILE.read_text().strip() if BUILD_FILE.is_file() else os.getenv('SERVING_RELEASE_SHA','local')
    sha=raw if re.fullmatch(r'[0-9a-f]{40}',raw) else None
    return {'server_version':sha[:12] if sha else 'local' if raw=='local' else 'unversioned',
            'release_sha':sha,'server_instance':INSTANCE}
