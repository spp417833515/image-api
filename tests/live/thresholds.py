"""Acceptance thresholds for the mask route, shared by every live test that measures it.

Mean |difference| per pixel, on 0–255, between the base image and the result:
- outside the white area: the user's limit; its single source is the production check of an upgrade
  (imagegen.biz.version_checks, ppwiki decision id=5). It holds for the backend's raw output; the result a user gets
  has the original pasted back there (imagegen.biz.masking), so through the HTTP API it is close to 0;
- inside the white area: at least this much, so the area really changed (tests only: at the upgrade check's small
  trial size the change inside is not reliably this large).
"""

from imagegen.biz.version_checks import MAX_MASK_OUTSIDE_DRIFT as MAX_OUTSIDE_DRIFT  # noqa: F401  (re-exported)

MIN_INSIDE_CHANGE = 10.0
