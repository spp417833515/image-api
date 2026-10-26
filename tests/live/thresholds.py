"""Acceptance thresholds for the mask route, shared by every live test that measures it.

Mean |difference| per pixel, on 0–255, between the base image and the result:
- outside the white area: the user's limit (ppwiki decision id=5, relaxed from 3 to 5 when the base image also went
  to the encoder; measured 1.53 on a photo, up to 4.25 on line art);
- inside the white area: at least this much, so the area really changed.
"""

MAX_OUTSIDE_DRIFT = 5.0
MIN_INSIDE_CHANGE = 10.0
