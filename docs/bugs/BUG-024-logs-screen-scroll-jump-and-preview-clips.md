# BUG-024: Logs screen jumped on open, startup warning sat top right, small preview images synced as clips

## Status
FIXED IN CODE (2026-11-13), unit tested for the preview guard. **OPEN until confirmed in everyday use.**

## Symptoms
1. Opening Logs after another screen showed the page slightly scrolled up, then it shifted down.
2. A "Reconnecting to CopyClip" pill appeared top right during startup, away from where every other notice appears.
3. The small preview Samsung puts on the clipboard around a screenshot (about 4 percent of the full size, 270x560 against 1080x2243) still reached the laptop and Activity as a clip of its own.

## Causes
1. Two things combined. `mount()` keeps the scroll offset so refreshes do not jump, but a new screen also inherited the previous screen's offset. And the log box grew from a 220 px "Loading" state to its full height once the lines arrived, so the page height changed under the offset. Not reproduced on demand, found by reading the code and measuring the heights.
2. The indicator was a fixed pill outside the toast stack, and showed on the first slow poll.
3. The Android side holds clipboard images for 4.5 seconds but misses some previews, and the PC had no guard of its own.

## Fix
1. A new screen starts at the top. The log box has a fixed height from the first paint.
2. The indicator is a toast in the bottom right stack with a spinner, shown only after 1.5 seconds without a reply.
3. `Hub._preview_gate`: an image under 60 KB waits 2.5 seconds. If a larger picture of the same shape (aspect ratio within 2 percent, at least twice the area) arrives from the same device, the preview is dropped and recorded as skipped. A preview that arrives after the full picture within 6 seconds is dropped at once. Other devices, other shapes and lone small images are unaffected (a lone small image reaches the clipboard 2.5 seconds late).

## Follow-up (same day): the remaining Logs flicker
Recorded frame by frame in the page (a 4 ms timer, reading the log box position after clicking Logs). The first frames showed the box at scroll position 0, the oldest lines, for about 70 ms, then it jumped to the bottom. The page built the box and asked it to scroll to the bottom before it was attached, where a scroll position cannot move. The scroll now runs again right after the page is on screen. After the fix the first frame is already at the bottom, and no other state appears.

## Follow-up (same day): preview guard behaviour
* Measured in the logs: 72 small images, 62 of them a preview followed by the full image (gap 0 to 1 s for 57, up to 4 s for the rest), 4 arriving after the full image, 6 standalone. The 2.5 second hold catches the large majority and only delays images under 60 KB, on the other devices (the source device already has its own copy).
* The dropped preview no longer gets a row of its own. The full picture's row says "ignored the small preview Samsung adds".
* New setting `filter_screenshot_previews` (default on) turns the guard off.

## New in the same change
* Activity rows show a thumbnail for pictures and a copy button at the right end. Text up to 20,000 characters is kept in memory (same switch as previews, never for sensitive clips) so it can be copied again.
* The tray flyout shows recent pictures as thumbnails and a **Copy full image** button, also in the tray flyout. The picture is served from the file CopyClip already saved, so no image library is needed and the sync path is untouched. The API never exposes the file path.

## Not verified
* A live screenshot on the S24 Ultra and the Tab S10 Ultra after the change.
* Memory use of many full-size pictures as thumbnails (the browser scales them down; there is no server-side thumbnail without an image library).
