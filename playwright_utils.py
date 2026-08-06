import os
import shutil


def resolve_browser_executable(explicit_path: str | None = None) -> str | None:
    """Return a browser executable path that Playwright can launch.

    Preference order:
    1. An explicit path passed in.
    2. The PLAYWRIGHT_BROWSER_EXECUTABLE environment variable.
    3. Common system browser installation paths.
    """
    if explicit_path:
        return explicit_path

    env_path = os.environ.get("PLAYWRIGHT_BROWSER_EXECUTABLE")
    if env_path:
        return env_path

    for candidate in (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if candidate and os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return (
        shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
    )


async def launch_playwright_browser(playwright, *, headless: bool = True):
    """Launch Chromium with a system browser fallback when no Playwright bundle is available."""
    launch_kwargs = {"headless": headless}
    executable = resolve_browser_executable()
    if executable:
        launch_kwargs["executable_path"] = executable

    # Some CI and sandboxed environments block Chromium's default isolation
    # features, so add conservative fallbacks that are still safe for this
    # read-only scraper use case.
    launch_kwargs["args"] = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--single-process",
        "--no-zygote",
    ]
    return await playwright.chromium.launch(**launch_kwargs)
