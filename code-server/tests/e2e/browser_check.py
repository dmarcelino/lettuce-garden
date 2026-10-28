"""Drives the workbench in Chromium for scripts/code-server-e2e.sh --browser: opens
agent-1's folder, a TypeScript file and a terminal (typing a command into it), asks
to run a task, and previews a planted README.md, also after choosing "Disable" in
the preview's security picker, the way a person would. What ran where is checked
afterwards by the script, from marker files; this prints what the page showed, and
whether the planted Markdown asked for anything (every request it would make has
/planted-md in its URL)."""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = "http://localhost:8000/code-server/?folder=/work/agent-1"


def main() -> None:
    ok = True
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        # The context sees the webviews' frames and their service worker too.
        # /control-md: a plain Markdown image, which the preview may load; seeing it
        # shows that requests from inside the preview are seen at all.
        planted: list[str] = []
        control: list[str] = []
        context.on("request", lambda r: planted.append(r.url) if "/planted-md" in r.url else None)
        context.on("request", lambda r: control.append(r.url) if "/control-md" in r.url else None)
        page = context.new_page()
        page.goto(URL)
        page.wait_for_selector(".monaco-workbench", timeout=120_000)
        page.wait_for_timeout(8_000)
        # A dialog here would take the keys below; and none should ask anything on open.
        asked_on_open = page.locator(".monaco-dialog-box").count() > 0
        print(f"{'FAIL' if asked_on_open else 'ok  '}  browser: opening the folder asks nothing")
        if asked_on_open:
            print("        " + page.locator(".monaco-dialog-box").first.inner_text().replace("\n", " ")[:300])
            sys.exit(1)
        restricted = page.get_by_text("Restricted Mode").count() > 0
        print(f"{'ok  ' if restricted else 'FAIL'}  browser: the workbench says Restricted Mode")
        ok &= restricted

        page.keyboard.press("Control+P")
        page.keyboard.type("index.ts")
        page.wait_for_timeout(1_500)
        page.keyboard.press("Enter")
        page.wait_for_timeout(15_000)

        page.keyboard.press("Control+Backquote")
        page.wait_for_timeout(10_000)
        quiet = page.locator(".monaco-dialog-box").count() == 0
        print(f"{'ok  ' if quiet else 'FAIL'}  browser: opening a terminal asks nothing")
        ok &= quiet
        if not quiet:
            page.keyboard.press("Escape")
        # The terminal has the focus: what is typed runs wherever its shell runs.
        page.keyboard.type("touch /tmp/typed-in-terminal\n")
        page.wait_for_timeout(3_000)

        page.keyboard.press("F1")
        page.wait_for_timeout(1_000)
        page.keyboard.type("Tasks: Run Task")
        page.wait_for_timeout(2_000)
        page.keyboard.press("Enter")
        page.wait_for_timeout(4_000)
        asked = page.locator(".monaco-dialog-box").filter(has_text="trust").count() > 0
        print(f"{'ok  ' if asked else 'FAIL'}  browser: running a task asks for trust first")
        ok &= asked
        page.keyboard.press("Escape")
        page.wait_for_timeout(2_000)

        def command(name: str) -> None:
            page.keyboard.press("F1")
            page.wait_for_timeout(1_000)
            page.keyboard.type(name)
            page.wait_for_timeout(1_500)
            page.keyboard.press("Enter")

        def preview_shows(text: str) -> bool:
            for frame in page.frames:
                try:
                    if text in frame.content():
                        return True
                except Exception:  # a frame that went away
                    continue
            return False

        page.keyboard.press("Control+P")
        page.keyboard.type("README.md")
        page.wait_for_timeout(1_500)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3_000)
        command("Markdown: Open Preview to the Side")
        page.wait_for_timeout(8_000)
        shown = preview_shows("Planted preview")
        print(f"{'ok  ' if shown else 'FAIL'}  browser: the Markdown preview renders")
        ok &= shown
        command("Markdown: Change Preview Security Settings")
        page.wait_for_timeout(1_500)
        page.keyboard.type("Disable")
        page.wait_for_timeout(1_000)
        page.keyboard.press("Enter")
        page.wait_for_timeout(8_000)
        shown = preview_shows("Planted preview")
        print(f"{'ok  ' if shown else 'FAIL'}  browser: the preview still renders after choosing Disable")
        ok &= shown
        seen = bool(control)
        print(f"{'ok  ' if seen else 'FAIL'}  browser: requests from inside the preview are seen (its image)")
        ok &= seen
        quiet = not planted
        print(f"{'ok  ' if quiet else 'FAIL'}  browser: the planted Markdown made no request, even with Disable")
        if not quiet:
            print("        " + " ".join(sorted(set(planted)))[:300])
        ok &= quiet
        browser.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
