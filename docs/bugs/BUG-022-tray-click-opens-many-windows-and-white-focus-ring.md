# BUG-022: Clicking the tray icon opened several browser windows, the window looked like a web page, and a white ring framed the content

## Status
FIXED IN CODE and verified on the maintainer's PC (2026-11-13). **OPEN until confirmed in everyday use.**

## Severity
Medium. Nothing is lost, but the first impression of the new window was a web page in a browser tab with a white frame.

## Symptoms (reported with a screenshot)
1. A bright white outline hugged the content pane, and the default white scrollbar showed at the right edge.
2. The window opened inside the person's normal browser (Brave) and read as a refreshed page rather than an app.
3. Clicking the tray icon, fast or slowly, opened more windows instead of showing the existing one.

## Root causes
1. The script gave the `<main>` container keyboard focus on every screen change. Chromium draws its default focus ring on it, and in a dark colour scheme that ring is white. The stylesheet's `outline: none` did not win everywhere.
2. The window launched in the default browser's normal profile, so it lived inside the daily browser process and window group, and it had a second, in-page title bar under the native one.
3. One left click on the tray icon can arrive as both `WM_LBUTTONUP` and `NIN_SELECT` (tray API version 4), and nothing remembered that a window was already being launched. A second request a moment later started another. Slow starts made it worse: the watcher gave up after 4 seconds and opened a different browser.

## Fix
- No container is ever focused. Focus rings appear only on controls, only for keyboard users. Scrollbars are themed.
- The window runs in a private profile (`%LOCALAPPDATA%\CopyClip\ui-profile\<browser>`): its own process tree, its own taskbar entry and the CopyClip icon (the icon file is now a multi-size 16 to 256 px set). The in-page title bar is gone. On a brand-new Brave profile the first-run analytics banner is pre-acknowledged.
- `windows_app/launcher.py` has one model for a single instance: if the window exists it is restored and brought to the front; if one is already opening, further clicks are ignored; only a browser that exits without producing a window is followed by the next one. Tray clicks inside 0.6 s count as one. Picking Settings or Logs from the tray menu switches an already-open window to that screen.
- A window whose tray has gone away closes itself after 25 seconds, so clicking the tray never focuses a dead window.

## Evidence
- Real window captured with `PrintWindow` on the maintainer's PC: native title bar with icon, no white ring, no banner.
- 16 rapid open requests (8 simultaneous, 8 sequential) left exactly one window.
- Minimising the window and requesting it again restored it, made it the foreground window, and left one window.
- `tests/unit/test_launcher.py::test_twenty_simultaneous_clicks_launch_exactly_once` and `tests/unit/test_status_tray.py::test_rapid_clicks_count_as_one`.

## Not verified
- Behaviour on a PC where the default browser is not Chromium based and none of Edge, Brave or Chrome work (falls back to a normal browser tab).
- Taskbar grouping on a second monitor and at other display scales.
