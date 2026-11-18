# BUG-015: Historical Screenshot and Photo Resync on File Deletion

## Status
CONFIRMED, fixed and verified live on hardware (2026-10-29).

## Severity
Medium-High (unwanted re-sync and duplicate history).

## Symptom
When a recently captured screenshot or camera photo was deleted on the phone or tablet, `ClipBridge` immediately emitted whatever older screenshot or camera photo remained on disk. An image captured hours or days ago would suddenly pop up on the Windows clipboard and save into `Temporary Screenshots`.

## Root Cause
`findNewestScreenshot()` and `findNewestCameraPhoto()` sort all files by modification time and return the highest. When the newest file was deleted, the pointer naturally reverted to the previous file.
In `checkScreenshots()` and `checkCameraPhotos()`, the comparison was purely:
`boolean sameName = lastScreenshotName != null && newest.getName().equals(lastScreenshotName);`
Because the previous file had a different name from the deleted file, `sameName` evaluated to false. `ClipBridge` assumed this was a newly taken capture and re-emitted it over the bridge.

## Fix
Added an age threshold check in both `checkScreenshots()` and `checkCameraPhotos()`:
```java
long ageMs = System.currentTimeMillis() - newest.lastModified();
if (ageMs > 120_000L && lastScreenshotName != null && !newest.getName().equals(lastScreenshotName)) {
    lastScreenshotName = newest.getName();
    lastScreenshotFirstSyncMs = System.currentTimeMillis();
    lastScreenshotLastSyncedLen = newest.length();
    return;
}
```
If the fallback file is older than 2 minutes, `ClipBridge` quietly updates its internal tracking baseline without broadcasting the historical file to Windows.

## Verification
Pushed a temporary screenshot to Tab S10 Ultra, confirmed sync, and then deleted the file via ADB `rm -f`. Verified in `logs/copyclip.log` that the older remaining screenshots on the tablet were ignored and never re-emitted.
