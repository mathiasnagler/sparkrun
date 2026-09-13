# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/sparkroute/LICENSE_EXCEPTION.

"""Child environments for the required Sparkrun application profile API."""

from __future__ import annotations

import os
from pathlib import Path

from sparkrun.core import application_profile


def child_environment(config_path: Path | None = None) -> dict[str, str]:
    """Same-controller children inherit process settings and profile/config identity."""
    environment = dict(os.environ)
    environment.update(application_profile.child_environment(config_path=config_path))
    return environment
