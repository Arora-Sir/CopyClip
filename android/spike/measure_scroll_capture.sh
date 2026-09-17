#!/system/bin/sh
# Throwaway measurement spike for BUG-014 (Samsung long screenshot / scroll capture partial
# sync). Not shipped, not referenced by ClipBridge.java. Exists only to answer, with real
# measured evidence, the question production code cannot be written against yet: what does
# Samsung's filesystem actually do to /sdcard/DCIM/Screenshots during a real scroll capture.
#
# Specifically: does the final stitched image land under the SAME filename as the initial
# single-page shot, or a new one; does that file grow incrementally while the user is still
# tapping "Capture more," or does it appear once, fully formed; and how long does the real
# final encode and flush actually take for a genuinely tall image (bugs/BUG-006 measured
# 2.8 to 3.5 seconds for a normal-sized screenshot, this is expected to take longer).
#
# Usage:
#   adb push measure_scroll_capture.sh /sdcard/measure_scroll_capture.sh
#   adb shell sh /sdcard/measure_scroll_capture.sh
# then, while it is running, physically perform a real long screenshot on the device.
# Afterward:
#   adb pull /sdcard/copyclip_spike_scroll.log
#
# Run twice: once with "Capture more" taps spaced closely together, once spaced several
# seconds apart, to see whether tap cadence changes the on-disk pattern. Optionally run a
# third time with the CopyClip daemon connected and running normally, to directly compare
# what ClipBridge actually shipped against what this log shows really happened on disk.
#
# Only /sdcard/DCIM/Screenshots is watched: ClipBridge.java's own header comment records
# that /sdcard/Pictures/Screenshots does not exist on this hardware, confirmed again here
# via a live listing before this script was written.

DIR=/sdcard/DCIM/Screenshots
OUT=/sdcard/copyclip_spike_scroll.log
DURATION_S=90

: > "$OUT"
echo "watching $DIR for ${DURATION_S}s, writing $OUT" >&2

end=$(( $(date +%s) + DURATION_S ))
while [ "$(date +%s)" -lt "$end" ]; do
    f=$(ls -t "$DIR" 2>/dev/null | head -1)
    if [ -n "$f" ]; then
        st=$(stat -c '%s %Y' "$DIR/$f" 2>/dev/null)
        tb=$(tail -c 32 "$DIR/$f" 2>/dev/null | xxd -p | tr -d '\n')
        echo "$(date +%s.%3N) $f $st $tb" >> "$OUT"
    fi
    sleep 0.1
done

echo "done, wrote $OUT" >&2
