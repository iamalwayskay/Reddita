from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from patchright.sync_api import sync_playwright

from reddita.cache import open_private_cache
from reddita.scraper import _open_browser_context


@unittest.skipUnless(
    os.environ.get("REDDITA_BROWSER_SMOKE") == "1",
    "set REDDITA_BROWSER_SMOKE=1 after installing Chromium",
)
class BrowserSmokeTests(unittest.TestCase):
    def test_managed_persistent_browser_launches_and_renders(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            profile = Path(directory) / "profile"
            descriptor = open_private_cache(profile)
            if isinstance(descriptor, int):
                os.close(descriptor)
            with sync_playwright() as playwright:
                context, browser = _open_browser_context(
                    playwright, profile_root=profile, allow_stealth=True,
                )
                try:
                    page = context.new_page()
                    page.set_content("<main>reddita browser ready</main>")
                    self.assertEqual(page.locator("main").inner_text(), "reddita browser ready")
                finally:
                    context.close()
                    if browser is not None:
                        browser.close()
