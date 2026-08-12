package me.mohit.copyclip;

import android.app.Application;
import android.app.Instrumentation;
import android.content.AttributionSource;
import android.content.ClipData;
import android.content.ClipDescription;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.IContentProvider;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Binder;
import android.os.Build;
import android.os.IBinder;
import android.os.Looper;
import android.os.PersistableBundle;
import android.os.Process;
import android.provider.MediaStore;
import android.provider.OpenableColumns;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.PrintStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicLong;

/**
 * CopyClip on-device clipboard bridge.
 *
 * Runs under the shell UID (2000) via app_process, which is the only non-root identity Android
 * grants background clipboard access to. Verified on this device: appops reports
 * "com.android.shell READ_CLIPBOARD: allow".
 *
 * Talks to android.content.IClipboard directly over Binder. Every framework call is resolved by
 * version-adaptive reflection rather than a fixed signature, because the parameter list has
 * changed repeatedly (attributionTag in Android 11, deviceId in Android 14). scrcpy issue #4285
 * is the cautionary tale here.
 *
 * Never run this as root. scrcpy issue #1095 documents the failure: root triggers
 * "SecurityException: Calling uid 0 does not own package com.android.shell".
 *
 * Wire protocol (stdio, line oriented, base64 payloads so newlines and Unicode survive intact):
 *
 *   out: READY <sdk>
 *        DESC <mime> <sensitive:0|1> <timestamp> <sha256> <charlen>
 *        STAMP <timestamp>          (change seen, content deliberately not read)
 *        CLIP <mime> <sensitive:0|1> <base64-utf8>
 *        SKIP <mime> <reason>
 *        LOCK <0|1>
 *        ACK <verb>
 *        DEFER <verb> <reason>
 *        PONG
 *        ERR <message>
 *        CLIPB <id> <mime> <sensitive:0|1> <sha256> <bytes> <nchunks>  (image header)
 *        CLIPC <id> <idx> <base64chunk>                                (image chunk)
 *   in:  SET <base64-utf8>
 *        GET          (send the body)
 *        DESC         (send metadata only)
 *        MODE stamp   (announce timestamps only, never read content)
 *        MODE full    (read content to produce DESC hashes)
 *        PING
 *        QUIT
 *        SETB <id> <mime> <sha256> <bytes> <nchunks>   (image header)
 *        SETC <id> <idx> <base64chunk>                  (image chunk)
 *
 * The poll loop announces changes with DESC, never CLIP. The laptop asks for the body with
 * GET only when it does not already recognise the hash, which stops Samsung's
 * phone-to-tablet mirroring from putting every clip on the wire twice.
 *
 * Reading clip *content* raises Android's "Shell pasted from your clipboard" toast, so
 * content reads are minimised aggressively: never for our own writes, and never at all on
 * a standby device whose Samsung sibling is already being read.
 *
 * Images use SETB/SETC and CLIPB/CLIPC instead of SET/CLIP because a multi-megabyte
 * payload as one line would block the reader/poll threads; text's SET/CLIP/GET/DESC path
 * above is completely unmodified by any of this. A ContentResolver is required to write an
 * image to the clipboard (a ClipData.Item needs a real content:// URI -- a file:// URI
 * throws FileUriExposedException), which under app_process needs the FakeContext trick
 * (ported from scrcpy, see bugs/SPIKE-IMAGE-CLIPBOARD.md for what was measured and the two
 * bugs found and fixed in that port). Text's setPrimaryClip/getPrimaryClip continue to go
 * through the plain reflective IClipboard binder above -- FakeContext is scoped narrowly to
 * ContentResolver, never used for the clipboard service itself.
 */
public final class ClipBridge {

    /** Must match the shell UID's real package or setPrimaryClip throws SecurityException. */
    private static final String CALLING_PKG = "com.android.shell";
    private static final int USER_ID = 0;
    private static final int DEVICE_ID = 0; // Context.DEVICE_ID_DEFAULT

    /** ClipDescription.EXTRA_IS_SENSITIVE, inlined so we do not hard-depend on API 33+. */
    private static final String EXTRA_IS_SENSITIVE = "android.content.extra.IS_SENSITIVE";

    private static final long POLL_MS = 250L;

    private static Object clipboard;
    private static Object trust;
    private static PrintStream out;

    /**
     * Measured on this device: while the keyguard is up, getPrimaryClip returns null and
     * setPrimaryClip is accepted but silently discarded. Screen state is irrelevant, only the
     * lock is. So a write that arrives while the phone is in your pocket is parked here and
     * replayed the moment the device unlocks, rather than being lost without a trace.
     */
    private static volatile String pendingSet;

    /**
     * Timestamp of the clip *we* last wrote.
     *
     * Reading clip content raises Android's "Shell pasted from your clipboard" toast, so
     * reading back our own write is both pointless and noisy: we already know the content.
     * The poll loop compares against this and skips the content read entirely when the
     * change was our own. `getPrimaryClipDescription` does not raise the toast, which is
     * what makes this check free.
     */
    private static volatile long ownWriteStamp = Long.MIN_VALUE;

    /**
     * When true this device announces changes as a bare timestamp and never reads content.
     *
     * Used for the standby member of a Samsung sync domain. Both devices mirror each other,
     * so only one of them needs to read a clip; the standby reading too would double both
     * the toasts and the work for a single copy. A copy made here still reaches the laptop,
     * because Samsung relays it to the representative where it is read once.
     */
    private static volatile boolean stampOnly = false;

    /**
     * Hash of content we just wrote on behalf of the laptop. The write bumps the clipboard
     * timestamp, so without this the poll loop would immediately echo it back. First line of
     * defence against loops; copyclip.py holds the second.
     */
    private static volatile String suppressHash;

    /**
     * Last time a line arrived from the laptop over stdin, any verb.
     *
     * The laptop pings every idle_ping_s (60s) when otherwise quiet, so this should never
     * drift far behind "now" on a healthy connection. When the network drops, readCommands()
     * only learns about it once the OS actually closes the pipe, which can lag noticeably
     * behind the network actually going down. Until then this process keeps polling and, if
     * still in full mode, keeps reading content -- see the orphan check in serve() below.
     */
    private static volatile long lastHubContact = System.currentTimeMillis();

    /**
     * How long without a word from the laptop before treating this process as orphaned and
     * suppressing content reads, same as stampOnly. Deliberately well above the laptop's own
     * 60s idle-ping cycle (plus its ~30s grace waiting on a PONG) so a completely healthy but
     * quiet connection never trips this by mistake.
     */
    private static final long IDLE_ORPHAN_MS = 100_000L;

    // ------------------------------------------------------------ imaging constants

    /** Raw bytes per chunk on both directions. Must match nodes/android_node.py's
     * CHUNK_BYTES for the inbound (laptop-initiated) direction; the outbound direction is
     * free to differ since reassembly just concatenates whatever arrives, but reusing the
     * same value keeps one thing to remember instead of two. */
    private static final int CHUNK_BYTES = 48 * 1024;

    /** Device ACKs every this-many chunks so the sender's stall-detection deadline resets
     * regardless of image size -- see nodes/android_node.py's ACK_WINDOW for the full
     * rationale; the two constants must agree for the pairing to land where the sender
     * expects it, though a mismatch would only cost a slightly early/late reset, not
     * correctness. */
    private static final int ACK_WINDOW = 8;

    /** Independent, device-side enforcement of the same cap core/policy.py applies on the
     * laptop -- belt and braces: a compromised or buggy laptop-side check must not be the
     * only thing standing between a malicious/huge SETB header and unbounded memory use
     * here. */
    private static final long MAX_IMAGE_BYTES = 25_165_824L; // 24 MiB

    /** Pass through byte-for-byte, no re-encode. Anything else (heic/heif/avif/unknown)
     * is transcoded to PNG -- see readImageBytes(). */
    private static final Set<String> PASSTHROUGH_MIMES = new HashSet<>(Arrays.asList(
            "image/png", "image/jpeg", "image/gif", "image/bmp", "image/webp"));

    private static final AtomicLong OUTBOUND_TRANSFER_SEQ = new AtomicLong();

    /** True once FakeContext's ContentResolver has been proven to construct without
     * throwing. Guards every image code path below; when false, images are skipped
     * (reads) or rejected (writes) but text sync is completely unaffected -- see
     * initImaging(). */
    private static volatile boolean imagingAvailable = false;

    /** Mirrors pendingSet, but for an image write that arrived while the device was
     * locked. Replayed by flushPending() the moment the device unlocks, exactly like
     * text's pendingSet. */
    private static volatile byte[] pendingImageData;
    private static volatile String pendingImageMime;

    /** What emitDesc()'s image branch last read and hashed, keyed by the clip's own
     * timestamp so a stale cache is never served for a newer clip. emitCurrentClip()'s
     * image branch reuses this instead of reading the URI a second time, since a GET
     * always follows a DESC the laptop just saw for the same clip. Marked volatile:
     * written on whichever thread emits the DESC (poll loop or GET handling), read on
     * whichever thread later handles GET -- these can be different threads. */
    private static volatile byte[] cachedImageBytes;
    private static volatile String cachedImageMime;
    private static volatile String cachedImageHash;
    private static volatile long cachedImageTs = Long.MIN_VALUE;

    /** Reassembly state for a laptop-initiated chunked image (SETB/SETC). Touched only
     * from the stdin-reader thread (readCommands()'s single loop), so -- like the rest of
     * that thread's command-handling state -- it needs no locking. */
    private static String inboundTransferId;
    private static String inboundMime;
    private static String inboundSha256;
    private static long inboundTotalBytes;
    private static int inboundNChunks;
    private static final List<String> inboundChunks = new ArrayList<>();

    public static void main(String[] args) {
        out = new PrintStream(new java.io.FileOutputStream(java.io.FileDescriptor.out), true,
                StandardCharsets.UTF_8);
        try {
            clipboard = connectClipboard();
        } catch (Throwable t) {
            emit("ERR connect: " + describe(t));
            System.exit(1);
            return;
        }
        initImaging(); // failure here must never affect text sync -- see its own try/catch

        String mode = args.length > 0 ? args[0] : "serve";
        try {
            switch (mode) {
                case "get":
                    emitCurrentClip(true);
                    break;
                case "desc":
                    emitDesc();
                    break;
                case "set":
                    if (args.length < 2) {
                        emit("ERR set requires a base64 argument");
                        System.exit(2);
                        return;
                    }
                    doSet(args[1]);
                    break;
                case "serve":
                    serve();
                    break;
                default:
                    emit("ERR unknown mode " + mode);
                    System.exit(2);
            }
        } catch (Throwable t) {
            emit("ERR " + mode + ": " + describe(t));
            System.exit(1);
        }
        System.exit(0);
    }

    // ---------------------------------------------------------------- binder

    private static Object service(String name, String stubClass) throws Exception {
        Class<?> sm = Class.forName("android.os.ServiceManager");
        IBinder binder = (IBinder) sm.getMethod("getService", String.class).invoke(null, name);
        if (binder == null) {
            throw new IllegalStateException(name + " service not found");
        }
        Class<?> stub = Class.forName(stubClass);
        return stub.getMethod("asInterface", IBinder.class).invoke(null, binder);
    }

    private static Object connectClipboard() throws Exception {
        Object cb = service("clipboard", "android.content.IClipboard$Stub");
        try {
            trust = service("trust", "android.app.trust.ITrustManager$Stub");
        } catch (Throwable t) {
            trust = null; // fall back to write-verify below
        }
        return cb;
    }

    // ------------------------------------------------------------ imaging bootstrap

    /**
     * Proves FakeContext's ContentResolver actually works on this device before any image
     * code path trusts it. Any failure here -- and there is real reason to expect one on
     * some OEM builds, see Shizuku-API's own warning quoted in bugs/SPIKE-IMAGE-CLIPBOARD.md
     * -- disables imaging and falls back to text-only, exactly as if this were an older
     * ClipBridge build. Never allowed to affect connectClipboard()'s already-working state.
     */
    private static void initImaging() {
        try {
            if (Looper.myLooper() == null) {
                // ActivityThread's constructor (inside FakeContext's static init) builds a
                // Handler, which throws unless this thread has a prepared Looper.
                // app_process's main thread has none by default. Measured directly --
                // bugs/SPIKE-IMAGE-CLIPBOARD.md.
                Looper.prepareMainLooper();
            }
            ContentResolver resolver = FakeContext.get().getContentResolver();
            if (resolver == null) {
                throw new IllegalStateException("FakeContext.getContentResolver() returned null");
            }
            imagingAvailable = true;
        } catch (Throwable t) {
            imagingAvailable = false;
            emit("ERR imaging: " + describe(t));
        }
    }

    /**
     * True when the keyguard is up. Prefers ITrustManager; if that is unavailable we report
     * unlocked and rely on the post-write verification in doSet to catch the failure anyway.
     */
    private static boolean isLocked() {
        if (trust == null) {
            return false;
        }
        try {
            Object r = call(trust, "isDeviceLocked", null);
            return r instanceof Boolean && (Boolean) r;
        } catch (Throwable t) {
            return false;
        }
    }

    /**
     * Invoke an IClipboard method without knowing its exact signature.
     *
     * Fills parameters positionally by type: first String is the calling package, second is the
     * attribution tag (null), first int is the user id, second is the device id, and any ClipData
     * slot takes the supplied clip. Overloads are tried widest first, so a newer signature wins on
     * a newer OS and we fall back gracefully on an older one.
     */
    private static Object call(Object target, String name, ClipData clip) throws Exception {
        List<Method> candidates = new ArrayList<>();
        for (Method m : target.getClass().getMethods()) {
            if (m.getName().equals(name)) {
                candidates.add(m);
            }
        }
        if (candidates.isEmpty()) {
            throw new NoSuchMethodException(name);
        }
        candidates.sort(Comparator.comparingInt((Method m) -> m.getParameterCount()).reversed());

        Throwable last = null;
        for (Method m : candidates) {
            try {
                return m.invoke(target, buildArgs(m, clip));
            } catch (java.lang.reflect.InvocationTargetException e) {
                Throwable cause = e.getCause();
                // A SecurityException means we reached the service and were refused. Trying other
                // overloads cannot help, so surface it rather than masking it behind a later
                // IllegalArgumentException.
                if (cause instanceof SecurityException) {
                    throw (SecurityException) cause;
                }
                last = cause != null ? cause : e;
            } catch (Throwable t) {
                last = t;
            }
        }
        throw new RuntimeException("no usable overload for " + name
                + " (" + candidates.size() + " tried): " + describe(last));
    }

    private static Object[] buildArgs(Method m, ClipData clip) {
        Class<?>[] types = m.getParameterTypes();
        Object[] args = new Object[types.length];
        int strSeen = 0, intSeen = 0;
        for (int i = 0; i < types.length; i++) {
            Class<?> t = types[i];
            if (t == String.class) {
                args[i] = (strSeen++ == 0) ? CALLING_PKG : null;
            } else if (t == int.class) {
                args[i] = (intSeen++ == 0) ? USER_ID : DEVICE_ID;
            } else if (ClipData.class.isAssignableFrom(t)) {
                args[i] = clip;
            } else if (t == boolean.class) {
                args[i] = Boolean.FALSE;
            } else if (t == long.class) {
                args[i] = 0L;
            } else if (t.isPrimitive()) {
                args[i] = 0;
            } else {
                args[i] = null;
            }
        }
        return args;
    }

    // ------------------------------------------------------------ clipboard

    private static ClipData getClip() throws Exception {
        return (ClipData) call(clipboard, "getPrimaryClip", null);
    }

    private static ClipDescription getDescription() throws Exception {
        return (ClipDescription) call(clipboard, "getPrimaryClipDescription", null);
    }

    /**
     * Writes the clipboard, or parks the payload if the device is locked.
     *
     * The write is verified by reading the description straight back, because a locked device
     * accepts setPrimaryClip and discards it without raising anything. Trusting the absent
     * exception is exactly how this fails silently.
     */
    private static void doSet(String b64) throws Exception {
        if (isLocked()) {
            pendingSet = b64;
            emit("DEFER set locked");
            return;
        }
        String text = new String(Base64.getDecoder().decode(b64), StandardCharsets.UTF_8);
        suppressHash = sha256(text);
        call(clipboard, "setPrimaryClip", ClipData.newPlainText("CopyClip", text));

        ClipDescription after = getDescription();
        if (after == null) {
            pendingSet = b64;
            emit("DEFER set discarded");
            return;
        }
        // Remember the stamp our own write produced so the poll loop does not read it back.
        ownWriteStamp = after.getTimestamp();
        pendingSet = null;
        emit("ACK set");
    }

    /** Replays a parked write once the device unlocks. */
    private static void flushPending() {
        String b64 = pendingSet;
        if (b64 != null && !isLocked()) {
            try {
                pendingSet = null;
                doSet(b64);
            } catch (Throwable t) {
                emit("ERR flush: " + describe(t));
            }
        }
        byte[] imageData = pendingImageData;
        if (imageData != null && !isLocked()) {
            String mime = pendingImageMime;
            try {
                pendingImageData = null;
                pendingImageMime = null;
                doSetImage(imageData, mime);
            } catch (Throwable t) {
                emit("ERR flush: " + describe(t));
            }
        }
    }

    private static boolean isSensitive(ClipDescription desc) {
        if (desc == null) {
            return false;
        }
        try {
            PersistableBundle extras = desc.getExtras();
            return extras != null && extras.getBoolean(EXTRA_IS_SENSITIVE, false);
        } catch (Throwable ignored) {
            return false; // older platform without extras support
        }
    }

    private static String mimeOf(ClipDescription desc) {
        if (desc == null || desc.getMimeTypeCount() == 0) {
            return "unknown";
        }
        return desc.getMimeType(0);
    }

    /**
     * Announce the current clip as metadata only: mime, sensitivity, timestamp, content
     * hash and length. No payload.
     *
     * This is what the poll loop emits on every change. With the phone and tablet inside
     * Samsung's sync domain, one copy surfaces on both of them, so shipping the body from
     * each would send a 93 KB clip across the wire twice for a single copy. The laptop
     * recognises an already-known hash and never asks for the body; it only issues GET for
     * a clip it has not seen, and only to whichever device reported it first.
     */
    private static void emitDesc() throws Exception {
        ClipDescription desc = getDescription();
        String mime = mimeOf(desc);

        if (isSensitive(desc)) {
            emit("SKIP " + mime + " sensitive");
            return;
        }
        if (isImageMime(mime)) {
            emitImageDesc(desc);
            return;
        }
        ClipData clip = getClip();
        if (clip == null || clip.getItemCount() == 0) {
            return;
        }
        CharSequence cs = clip.getItemAt(0).getText();
        if (cs == null) {
            return;
        }
        String text = cs.toString();
        String hash = sha256(text);
        if (hash.equals(suppressHash)) {
            return; // our own write coming back around
        }
        emit("DESC " + mime + " 0 " + (desc == null ? 0L : desc.getTimestamp())
                + " " + hash + " " + text.length());
    }

    /** Returns the sha256 of what was emitted, or null when nothing was emitted. */
    private static String emitCurrentClip(boolean force) throws Exception {
        ClipDescription desc = getDescription();
        String mime = mimeOf(desc);

        if (isSensitive(desc)) {
            emit("SKIP " + mime + " sensitive");
            return null;
        }
        if (isImageMime(mime)) {
            return emitCurrentImage(desc, force);
        }
        if (desc != null && !desc.hasMimeType(ClipDescription.MIMETYPE_TEXT_PLAIN)
                && !desc.hasMimeType(ClipDescription.MIMETYPE_TEXT_HTML)) {
            // Text and images (above) are both handled; anything else (files, ...) lands
            // here.
            emit("SKIP " + mime + " non-text");
            return null;
        }

        ClipData clip = getClip();
        if (clip == null || clip.getItemCount() == 0) {
            if (force) {
                emit("SKIP " + mime + " empty");
            }
            return null;
        }
        CharSequence cs = clip.getItemAt(0).getText();
        if (cs == null) {
            if (force) {
                emit("SKIP " + mime + " no-text-item");
            }
            return null;
        }

        String text = cs.toString();
        String hash = sha256(text);
        if (!force && hash.equals(suppressHash)) {
            return null; // our own write coming back around
        }
        emit("CLIP " + mime + " " + (isSensitive(desc) ? 1 : 0) + " "
                + Base64.getEncoder().encodeToString(text.getBytes(StandardCharsets.UTF_8)));
        return hash;
    }

    // ---------------------------------------------------------------- serve

    private static void serve() throws Exception {
        emit("READY " + android.os.Build.VERSION.SDK_INT);
        initScreenshotWatch();

        Thread stdin = new Thread(ClipBridge::readCommands, "copyclip-stdin");
        stdin.setDaemon(true);
        stdin.start();

        long lastStamp = -1L;
        boolean lastLocked = !isLocked(); // force an initial LOCK report

        while (true) {
            try {
                // Unconditional: a locked device still takes screenshots (they're saved by
                // the system screenshot service, not by anything gated on the keyguard),
                // and unlike clipboard content there's nothing here that a locked-device
                // read would silently discard -- this is a plain file read, not
                // getPrimaryClip(). Runs every tick regardless of stampOnly too -- see
                // checkScreenshots()'s own comment for why.
                checkScreenshots();

                boolean locked = isLocked();
                if (locked != lastLocked) {
                    lastLocked = locked;
                    emit("LOCK " + (locked ? 1 : 0));
                    if (!locked) {
                        // Unlocking makes the clipboard readable again and invalidates the
                        // "empty" we saw while locked, so resync from scratch.
                        lastStamp = -1L;
                        flushPending();
                    }
                }
                if (locked) {
                    Thread.sleep(POLL_MS);
                    continue; // reads return null while locked; nothing useful to do
                }

                ClipDescription desc = getDescription();
                // Polling the description is cheap: it never transfers clip content and
                // never raises a read notification. Only a changed timestamp justifies
                // looking at the clip at all.
                long stamp = desc == null ? 0L : desc.getTimestamp();
                if (stamp != lastStamp) {
                    lastStamp = stamp;
                    if (stamp == ownWriteStamp) {
                        // Our own write echoing back. Reading it would tell us nothing we
                        // do not already know, and would raise a "Shell pasted from your
                        // clipboard" toast for a clip the user never copied.
                        emit("STAMP " + stamp);
                    } else if (stampOnly) {
                        emit("STAMP " + stamp);
                    } else if (System.currentTimeMillis() - lastHubContact > IDLE_ORPHAN_MS) {
                        // Orphaned: the laptop has been silent well past its own idle-ping
                        // cycle, most likely because the network dropped and this process
                        // just hasn't been torn down yet. Treat like stampOnly until contact
                        // resumes, rather than reading content nobody can currently receive.
                        emit("STAMP " + stamp);
                    } else {
                        emitDesc();
                    }
                }
            } catch (Throwable t) {
                emit("ERR poll: " + describe(t));
            }
            Thread.sleep(POLL_MS);
        }
    }

    private static void readCommands() {
        try (BufferedReader in = new BufferedReader(
                new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
            String line;
            while ((line = in.readLine()) != null) {
                lastHubContact = System.currentTimeMillis();
                line = line.trim();
                if (line.isEmpty()) {
                    continue;
                }
                try {
                    int sp = line.indexOf(' ');
                    String verb = sp < 0 ? line : line.substring(0, sp);
                    String rest = sp < 0 ? "" : line.substring(sp + 1).trim();
                    switch (verb) {
                        case "SET":
                            doSet(rest);
                            break;
                        case "GET":
                            emitCurrentClip(true);
                            break;
                        case "DESC":
                            emitDesc();
                            break;
                        case "SETB":
                            handleSetB(rest);
                            break;
                        case "SETC":
                            handleSetC(rest);
                            break;
                        case "MODE":
                            // "MODE stamp" for a standby domain member, "MODE full" for the
                            // one device that actually reads clip content.
                            stampOnly = rest.equalsIgnoreCase("stamp");
                            emit("ACK mode " + (stampOnly ? "stamp" : "full"));
                            break;
                        case "PING":
                            emit("PONG");
                            break;
                        case "QUIT":
                            System.exit(0);
                            return;
                        default:
                            emit("ERR unknown verb " + verb);
                    }
                } catch (Throwable t) {
                    emit("ERR cmd: " + describe(t));
                }
            }
        } catch (Throwable t) {
            emit("ERR stdin: " + describe(t));
        }
        // Parent closed the pipe: the daemon is gone, so we should go too.
        System.exit(0);
    }

    // ---------------------------------------------------------------- imaging (FakeContext)

    /**
     * Ported from otherCopyProjects/scrcpy FakeContext.java + Workarounds.java, scoped
     * narrowly to obtaining a working ContentResolver under app_process -- the clipboard
     * itself is never touched through this class; setPrimaryClip/getPrimaryClip stay on the
     * proven reflective IClipboard-binder path above.
     *
     * Two fixes beyond a straight port, both found and measured on this project's own
     * hardware (bugs/SPIKE-IMAGE-CLIPBOARD.md), neither of which scrcpy itself needed
     * because it never does file I/O through this resolver:
     *   1. initImaging() prepares a main Looper first -- ActivityThread's constructor
     *      (below, in the static initializer) builds a Handler, which throws without one.
     *   2. acquireUnstableProvider() delegates for real instead of returning null.
     *      ContentResolver#openAssetFileDescriptor (which openInputStream/openOutputStream
     *      both go through) calls acquireUnstableProvider FIRST and throws immediately on
     *      a null return, rather than falling back to acquireProvider.
     */
    private static final class FakeContext extends ContextWrapper {
        static final String PACKAGE_NAME = "com.android.shell";
        static final int ROOT_UID = 0;

        private static final Class<?> ACTIVITY_THREAD_CLASS;
        private static final Object ACTIVITY_THREAD;

        static {
            try {
                ACTIVITY_THREAD_CLASS = Class.forName("android.app.ActivityThread");
                Constructor<?> ctor = ACTIVITY_THREAD_CLASS.getDeclaredConstructor();
                ctor.setAccessible(true);
                ACTIVITY_THREAD = ctor.newInstance();

                Field sCurrent = ACTIVITY_THREAD_CLASS.getDeclaredField("sCurrentActivityThread");
                sCurrent.setAccessible(true);
                sCurrent.set(null, ACTIVITY_THREAD);

                Field mSystemThread = ACTIVITY_THREAD_CLASS.getDeclaredField("mSystemThread");
                mSystemThread.setAccessible(true);
                mSystemThread.setBoolean(ACTIVITY_THREAD, true);
            } catch (Exception e) {
                throw new AssertionError(e);
            }
        }

        private static final FakeContext INSTANCE = new FakeContext();

        static FakeContext get() {
            return INSTANCE;
        }

        private final ContentResolver contentResolver = new ContentResolver(this) {
            // @Override (but super-class method not visible to javac against the stock SDK
            // jar -- android.content.ContentResolver#acquireProvider etc. are @hide.)
            protected IContentProvider acquireProvider(Context c, String name) {
                return ExternalProviders.get(name);
            }
            // @Override (but super-class method not visible)
            public boolean releaseProvider(IContentProvider icp) {
                return false;
            }
            // @Override (but super-class method not visible)
            //
            // scrcpy's own FakeContext always returns null here, because scrcpy never does
            // file I/O through this resolver. NOT safe to copy verbatim: measured directly,
            // ContentResolver#openAssetFileDescriptor calls this FIRST and throws "No
            // content provider: <uri>" immediately on a null return -- it does not fall
            // back to acquireProvider(). openOutputStream/openInputStream, exactly what
            // image sync needs, go through that path, so this must delegate for real.
            protected IContentProvider acquireUnstableProvider(Context c, String name) {
                return ExternalProviders.get(name);
            }
            // @Override (but super-class method not visible)
            public boolean releaseUnstableProvider(IContentProvider icp) {
                return false;
            }
            // @Override (but super-class method not visible)
            public void unstableProviderDied(IContentProvider icp) {
                // ignore
            }
        };

        private FakeContext() {
            super(getSystemContext());
        }

        @Override
        public String getPackageName() {
            return PACKAGE_NAME;
        }

        @Override
        public String getOpPackageName() {
            return PACKAGE_NAME;
        }

        @Override
        public AttributionSource getAttributionSource() {
            if (Build.VERSION.SDK_INT < 31) {
                return null;
            }
            AttributionSource.Builder b = new AttributionSource.Builder(Process.SHELL_UID);
            b.setPackageName(PACKAGE_NAME);
            return b.build();
        }

        @Override
        public Context getApplicationContext() {
            return this;
        }

        @Override
        public Context createPackageContext(String pkg, int flags) {
            return this;
        }

        @Override
        public ContentResolver getContentResolver() {
            return contentResolver;
        }

        private static void fillConfigurationController() {
            // "On some Samsung devices, DisplayManagerGlobal.getDisplayInfoLocked() calls
            // ActivityThread.currentActivityThread().getConfiguration(), which requires a
            // non-null ConfigurationController." -- scrcpy issue #4467. Both our target
            // devices (S24 Ultra, Tab S10 Ultra) are exactly this class of device.
            try {
                Class<?> ccClass = Class.forName("android.app.ConfigurationController");
                Class<?> atInternal = Class.forName("android.app.ActivityThreadInternal");
                Constructor<?> ctor = ccClass.getDeclaredConstructor(atInternal);
                ctor.setAccessible(true);
                Object cc = ctor.newInstance(ACTIVITY_THREAD);
                Field f = ACTIVITY_THREAD_CLASS.getDeclaredField("mConfigurationController");
                f.setAccessible(true);
                f.set(ACTIVITY_THREAD, cc);
            } catch (Throwable ignored) {
                // Best-effort workaround; absence is not fatal on every OEM/version.
            }
        }

        private static Context getSystemContext() {
            if (Build.VERSION.SDK_INT >= 31) {
                fillConfigurationController();
            }
            try {
                Method m = ACTIVITY_THREAD_CLASS.getDeclaredMethod("getSystemContext");
                return (Context) m.invoke(ACTIVITY_THREAD);
            } catch (Throwable t) {
                throw new RuntimeException("getSystemContext failed: " + describe(t), t);
            }
        }
    }

    /** Minimal port of scrcpy's ActivityManager wrapper: only getContentProviderExternal,
     * which is what lets FakeContext's ContentResolver reach any provider by authority
     * without holding a per-URI grant -- the shell UID's own permission is the check, not
     * a grant. */
    private static final class ExternalProviders {
        private static Object manager;
        private static Method getContentProviderExternalMethod;
        private static boolean newVersion = true;

        static synchronized IContentProvider get(String authority) {
            try {
                if (manager == null) {
                    Class<?> cls = Class.forName("android.app.ActivityManagerNative");
                    Method getDefault = cls.getDeclaredMethod("getDefault");
                    manager = getDefault.invoke(null);
                }
                if (getContentProviderExternalMethod == null) {
                    try {
                        getContentProviderExternalMethod = manager.getClass().getMethod(
                                "getContentProviderExternal", String.class, int.class,
                                IBinder.class, String.class);
                    } catch (NoSuchMethodException e) {
                        getContentProviderExternalMethod = manager.getClass().getMethod(
                                "getContentProviderExternal", String.class, int.class,
                                IBinder.class);
                        newVersion = false;
                    }
                }
                Object[] args = newVersion
                        ? new Object[]{authority, FakeContext.ROOT_UID, new Binder(), null}
                        : new Object[]{authority, FakeContext.ROOT_UID, new Binder()};
                Object holder = getContentProviderExternalMethod.invoke(manager, args);
                if (holder == null) {
                    return null;
                }
                Field providerField = holder.getClass().getDeclaredField("provider");
                providerField.setAccessible(true);
                return (IContentProvider) providerField.get(holder);
            } catch (Exception e) {
                return null;
            }
        }
    }

    // ------------------------------------------------------------ image read (device -> laptop)

    private static boolean isImageMime(String mime) {
        return mime != null && mime.toLowerCase(Locale.ROOT).startsWith("image/");
    }

    /**
     * Reads the bytes an image clip URI points to, applying the pass-through/transcode
     * rule: PNG/JPEG/GIF/BMP/WEBP travel byte-for-byte, original encoding preserved;
     * anything else (heic/heif/avif/unknown) is transcoded to PNG via the platform's own
     * decoder -- BitmapFactory/Bitmap need no Context and no hidden API, unlike everything
     * else in this file.
     *
     * Size-gated before reading: OpenableColumns.SIZE is checked first when the provider
     * supports it, and readAllBounded() enforces the cap on the actual byte count either
     * way, so a misreporting or SIZE-less provider still cannot exhaust memory.
     *
     * Returns {byte[] data, String mime}. Throws on any failure -- callers must treat that
     * as "skip this image", per bugs/SPIKE-IMAGE-CLIPBOARD.md's Q5/Q7 finding that reading
     * a clip URI minted by a different app's provider is not proven to always work for the
     * shell UID.
     */
    private static Object[] readImageBytes(Uri uri, String sourceMime) throws Exception {
        ContentResolver resolver = FakeContext.get().getContentResolver();

        long declaredSize = -1;
        try (Cursor c = resolver.query(uri, new String[]{OpenableColumns.SIZE}, null, null, null)) {
            if (c != null && c.moveToFirst()) {
                int idx = c.getColumnIndex(OpenableColumns.SIZE);
                if (idx >= 0 && !c.isNull(idx)) {
                    declaredSize = c.getLong(idx);
                }
            }
        } catch (Throwable ignored) {
            // Not every provider supports OpenableColumns; readAllBounded() below still
            // gates on the actual byte count regardless.
        }
        if (declaredSize > MAX_IMAGE_BYTES) {
            throw new IllegalStateException("declared size " + declaredSize + " exceeds cap " + MAX_IMAGE_BYTES);
        }

        byte[] raw;
        try (InputStream is = resolver.openInputStream(uri)) {
            if (is == null) {
                throw new IllegalStateException("openInputStream returned null for " + uri);
            }
            raw = readAllBounded(is, MAX_IMAGE_BYTES);
        }

        return applyPassthroughOrTranscode(raw, sourceMime);
    }

    /**
     * The pass-through/transcode decision, factored out so both the clipboard-URI path
     * above and the screenshot-file path below (§ checkScreenshots) share one
     * implementation rather than two copies that could drift.
     */
    private static Object[] applyPassthroughOrTranscode(byte[] raw, String mimeGuess) throws Exception {
        String mimeLower = mimeGuess == null ? "" : mimeGuess.toLowerCase(Locale.ROOT);
        if (PASSTHROUGH_MIMES.contains(mimeLower)) {
            return new Object[]{raw, mimeLower};
        }
        Bitmap bitmap = BitmapFactory.decodeByteArray(raw, 0, raw.length);
        if (bitmap == null) {
            throw new IllegalStateException("BitmapFactory could not decode " + mimeLower);
        }
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, out);
            return new Object[]{out.toByteArray(), "image/png"};
        } finally {
            bitmap.recycle();
        }
    }

    private static byte[] readAllBounded(InputStream is, long cap) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[65536];
        long total = 0;
        int n;
        while ((n = is.read(buf)) != -1) {
            total += n;
            if (total > cap) {
                throw new IllegalStateException("image exceeds cap while reading (" + total + " > " + cap + ")");
            }
            bos.write(buf, 0, n);
        }
        return bos.toByteArray();
    }

    // ------------------------------------------------------------ screenshot-folder watch

    /**
     * Samsung's screenshot capture does not call setPrimaryClip() -- measured directly:
     * bugs/SCREENSHOT-SYNC-FIX.md, three real screenshots taken while this poll loop was
     * running produced zero clipboard events. The "clipboard panel" showing a recent
     * screenshot is a convenience suggestion list, not the actual system clipboard. So
     * there is no clip event to catch here; instead this watches the screenshot folder
     * directly for new files, using the exact same poll-every-tick approach already used
     * for clipboard timestamps -- no FileObserver, no hidden API, consistent with the rest
     * of this file's philosophy.
     *
     * Confirmed via `adb shell` on this hardware: /sdcard/DCIM/Screenshots (NOT
     * Pictures/Screenshots, which does not exist on this device), identical path on both
     * phone and tablet, and the shell UID has full read access to both the listing and
     * file contents via its sdcard_rw/sdcard_r group membership -- no ContentResolver, no
     * FakeContext needed, plain java.io.File/FileInputStream.
     */
    private static final File SCREENSHOT_DIR = new File("/sdcard/DCIM/Screenshots");

    private static String lastScreenshotName;

    // Recently-pushed screenshot hashes, so a rare case where Samsung's own screenshot flow
    // produces two on-disk files for one real capture (different filenames -- e.g. a
    // scrolling-screenshot preview-then-final pair) doesn't get pushed twice. Defense in
    // depth on top of checkScreenshots()'s filename-identity check above: that check
    // handles the *measured*, common case (same file rewritten in place by a background
    // process -- see bugs/BUG-005-duplicate-screenshot-push.md); this handles a
    // same-content-different-filename case that check structurally can't catch. Not a
    // substitute for suppressHash below -- that's a single slot for the device's own most
    // recent *clipboard* write and is trivially clobbered by any other doSet in between.
    private static final long SCREENSHOT_DEDUP_WINDOW_MS = 20_000;
    private static final Map<String, Long> recentScreenshotHashes = new LinkedHashMap<String, Long>() {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, Long> eldest) {
            return size() > 32;
        }
    };

    private static synchronized boolean recentlyPushedScreenshot(String hash) {
        long now = System.currentTimeMillis();
        Long last = recentScreenshotHashes.put(hash, now);
        return last != null && (now - last) < SCREENSHOT_DEDUP_WINDOW_MS;
    }

    /**
     * Establishes the starting baseline silently -- called once before the poll loop
     * starts, so the newest screenshot already on disk when ClipBridge launches is never
     * announced as if it just appeared. This is deliberately a *local* baseline, not the
     * hub's existing on_node_connected() baseline mechanism: that mechanism is single-shot
     * per node (discards itself after the first clip it sees), so a device that can emit
     * two independent "first" signals -- current clipboard content, and current newest
     * screenshot -- would have the second one wrongly treated as a genuine new clip to
     * sync. Keeping this baseline entirely on the device side avoids that collision.
     */
    private static void initScreenshotWatch() {
        File newest = findNewestScreenshot();
        if (newest != null) {
            lastScreenshotName = newest.getName();
        }
    }

    private static File findNewestScreenshot() {
        File[] files = SCREENSHOT_DIR.listFiles();
        if (files == null || files.length == 0) {
            return null;
        }
        File newest = null;
        for (File f : files) {
            if (!f.isFile()) {
                continue;
            }
            if (newest == null || f.lastModified() > newest.lastModified()) {
                newest = f;
            }
        }
        return newest;
    }

    /**
     * Called on every poll tick, unconditionally -- deliberately NOT gated behind
     * stampOnly the way clipboard content reads are. Samsung Continuity relays clipboard
     * *content* between phone and tablet, so only one domain member needs to read it; it
     * does not relay arbitrary new *files*, so each device must watch its own screenshot
     * folder independently regardless of which one is the domain's current clipboard
     * representative.
     */
    private static void checkScreenshots() {
        try {
            File newest = findNewestScreenshot();
            if (newest == null) {
                return;
            }
            // Filename identity only -- lastModified() used to be part of this check too,
            // but a background process (media scanner, gallery thumbnail regen, cloud
            // backup) can rewrite the current newest file's bytes in place without any real
            // new screenshot happening: measured directly, same file size, different
            // sha256, new mtime, every time (bugs/BUG-005-duplicate-screenshot-push.md).
            // That used to look like a fresh screenshot and re-pushed the same shot
            // repeatedly. The filename is what actually identifies "which screenshot" --
            // Android's own timestamp-based naming already guarantees a genuinely new
            // capture gets a new name.
            if (lastScreenshotName != null && newest.getName().equals(lastScreenshotName)) {
                return;
            }
            lastScreenshotName = newest.getName();
            emitScreenshot(newest);
        } catch (Throwable t) {
            emit("ERR screenshot: " + describe(t));
        }
    }

    /**
     * Reads a newly-detected screenshot file directly and routes it through the same
     * chunked CLIPB/CLIPC machinery a clipboard-sourced image uses (sendChunkedClip) --
     * this is a new *trigger*, not a new pipeline. No DESC-then-GET two-step: unlike
     * clipboard content, a screenshot file is never redundantly re-announced by a Samsung
     * Continuity relay, so there is no "wait for a GET" step to save work on.
     */
    private static void emitScreenshot(File file) {
        // Deliberately NOT gated on imagingAvailable: unlike clipboard-URI images, this
        // path never touches FakeContext/ContentResolver at all -- it's a plain file read
        // via java.io.File/FileInputStream. If FakeContext ever fails to initialize (some
        // OEM quirk), screenshot watching should keep working regardless; they are
        // independent capabilities that merely share the outbound chunked-CLIP wire format.
        long size = file.length();
        if (size <= 0 || size > MAX_IMAGE_BYTES) {
            emit("SKIP image/unknown screenshot-oversize-or-empty");
            return;
        }
        byte[] raw;
        try (InputStream is = new FileInputStream(file)) {
            raw = readAllBounded(is, MAX_IMAGE_BYTES);
        } catch (Exception e) {
            emit("SKIP image/unknown screenshot-read-failed:" + describe(e));
            return;
        }
        byte[] data;
        String outMime;
        try {
            Object[] r = applyPassthroughOrTranscode(raw, sniffImageMime(raw));
            data = (byte[]) r[0];
            outMime = (String) r[1];
        } catch (Exception e) {
            emit("SKIP image/unknown screenshot-transcode-failed:" + describe(e));
            return;
        }
        String hash = sha256Bytes(data);
        if (hash.equals(suppressHash)) {
            return; // matches something we just wrote to this device ourselves
        }
        if (recentlyPushedScreenshot(hash)) {
            return; // same visual content pushed moments ago under a different filename
        }
        sendChunkedClip(outMime, false, hash, data);
    }

    /**
     * Detects format from magic bytes rather than assuming JPEG (the observed format for
     * Samsung's own screenshots) -- a screenshot tool or Android version could plausibly
     * save PNG instead, and guessing wrong would silently corrupt the mime tag the laptop
     * relies on to pick a decode path.
     */
    private static String sniffImageMime(byte[] data) {
        if (data.length >= 3 && (data[0] & 0xFF) == 0xFF && (data[1] & 0xFF) == 0xD8
                && (data[2] & 0xFF) == 0xFF) {
            return "image/jpeg";
        }
        if (data.length >= 8 && data[0] == (byte) 0x89 && data[1] == 'P' && data[2] == 'N'
                && data[3] == 'G') {
            return "image/png";
        }
        if (data.length >= 6 && data[0] == 'G' && data[1] == 'I' && data[2] == 'F') {
            return "image/gif";
        }
        if (data.length >= 2 && data[0] == 'B' && data[1] == 'M') {
            return "image/bmp";
        }
        if (data.length >= 12 && data[8] == 'W' && data[9] == 'E' && data[10] == 'B'
                && data[11] == 'P') {
            return "image/webp";
        }
        return "image/unknown";
    }

    /**
     * Announce an image clip the same way emitDesc() announces text: metadata only, no
     * body. Reads and hashes the *final* bytes (post pass-through/transcode decision) and
     * caches them, keyed by this clip's timestamp, so a following GET (emitCurrentClip)
     * does not read the URI a second time.
     */
    private static void emitImageDesc(ClipDescription desc) throws Exception {
        if (!imagingAvailable) {
            emit("SKIP " + mimeOf(desc) + " imaging-unavailable");
            return;
        }
        ClipData clip = getClip();
        if (clip == null || clip.getItemCount() == 0) {
            return;
        }
        Uri uri = clip.getItemAt(0).getUri();
        if (uri == null) {
            emit("SKIP " + mimeOf(desc) + " no-uri");
            return;
        }

        byte[] data;
        String outMime;
        try {
            Object[] r = readImageBytes(uri, desc.getMimeType(0));
            data = (byte[]) r[0];
            outMime = (String) r[1];
        } catch (SecurityException se) {
            emit("SKIP " + mimeOf(desc) + " unreadable-uri");
            return;
        } catch (Exception e) {
            emit("SKIP " + mimeOf(desc) + " read-failed:" + describe(e));
            return;
        }

        String hash = sha256Bytes(data);
        if (hash.equals(suppressHash)) {
            return; // our own write coming back around
        }
        cachedImageBytes = data;
        cachedImageMime = outMime;
        cachedImageHash = hash;
        cachedImageTs = desc.getTimestamp();
        emit("DESC " + outMime + " " + (isSensitive(desc) ? 1 : 0) + " "
                + desc.getTimestamp() + " " + hash + " " + data.length);
    }

    /** Returns the sha256 emitted, or null when nothing was emitted -- mirrors
     * emitCurrentClip()'s text contract exactly. */
    private static String emitCurrentImage(ClipDescription desc, boolean force) throws Exception {
        if (!imagingAvailable) {
            if (force) {
                emit("SKIP " + mimeOf(desc) + " imaging-unavailable");
            }
            return null;
        }
        ClipData clip = getClip();
        if (clip == null || clip.getItemCount() == 0) {
            if (force) {
                emit("SKIP " + mimeOf(desc) + " empty");
            }
            return null;
        }
        Uri uri = clip.getItemAt(0).getUri();
        if (uri == null) {
            if (force) {
                emit("SKIP " + mimeOf(desc) + " no-uri");
            }
            return null;
        }

        byte[] data;
        String outMime;
        long ts = desc == null ? 0L : desc.getTimestamp();
        if (cachedImageHash != null && ts == cachedImageTs) {
            // What emitImageDesc() already read and hashed for this exact clip -- a GET
            // always follows a DESC the laptop just saw, so re-reading would be redundant.
            data = cachedImageBytes;
            outMime = cachedImageMime;
        } else {
            try {
                Object[] r = readImageBytes(uri, desc.getMimeType(0));
                data = (byte[]) r[0];
                outMime = (String) r[1];
            } catch (Exception e) {
                if (force) {
                    emit("SKIP " + mimeOf(desc) + " read-failed:" + describe(e));
                }
                return null;
            }
        }

        String hash = sha256Bytes(data);
        if (!force && hash.equals(suppressHash)) {
            return null; // our own write coming back around
        }
        sendChunkedClip(outMime, isSensitive(desc), hash, data);
        return hash;
    }

    private static void sendChunkedClip(String mime, boolean sensitive, String hash, byte[] data) {
        String id = "dev-" + OUTBOUND_TRANSFER_SEQ.incrementAndGet();
        String b64 = Base64.getEncoder().encodeToString(data);
        int chunkChars = ((CHUNK_BYTES + 2) / 3) * 4;
        int nchunks = Math.max(1, (b64.length() + chunkChars - 1) / chunkChars);
        emit("CLIPB " + id + " " + mime + " " + (sensitive ? 1 : 0) + " " + hash + " "
                + data.length + " " + nchunks);
        for (int idx = 0, start = 0; start < b64.length(); idx++, start += chunkChars) {
            int end = Math.min(start + chunkChars, b64.length());
            emit("CLIPC " + id + " " + idx + " " + b64.substring(start, end));
        }
    }

    // ------------------------------------------------------------ image write (laptop -> device)

    /**
     * Applies an already-reassembled, sha-verified image to the clipboard, or parks it if
     * the device is locked. Mirrors doSet()'s structure exactly: the write is verified by
     * reading the description straight back, because a locked device accepts
     * setPrimaryClip and discards it without raising anything -- the same silent-discard
     * behaviour measured for text (NOTES.md) and re-confirmed for images
     * (bugs/SPIKE-IMAGE-CLIPBOARD.md).
     */
    private static void doSetImage(byte[] data, String mime) throws Exception {
        if (!imagingAvailable) {
            emit("ERR chunk imaging-unavailable");
            return;
        }
        if (isLocked()) {
            pendingImageData = data;
            pendingImageMime = mime;
            emit("DEFER set locked");
            return;
        }

        Uri uri = insertImage(data, mime);
        suppressHash = sha256Bytes(data);
        ClipDescription desc = new ClipDescription("CopyClip", new String[]{mime});
        ClipData clip = new ClipData(desc, new ClipData.Item(uri));
        call(clipboard, "setPrimaryClip", clip);

        ClipDescription after = getDescription();
        if (after == null) {
            pendingImageData = data;
            pendingImageMime = mime;
            emit("DEFER set discarded");
            return;
        }
        ownWriteStamp = after.getTimestamp();
        pendingImageData = null;
        pendingImageMime = null;
        emit("ACK set");
    }

    /**
     * MediaStore insert + write, kept under Pictures/CopyClip so any app with
     * READ_MEDIA_IMAGES can paste it -- maximum compatibility, per the implementation
     * plan's storage decision. History is kept (no delete-on-next-write): auto-cleanup is
     * explicitly out of scope for this version.
     */
    private static Uri insertImage(byte[] data, String mime) throws Exception {
        ContentResolver resolver = FakeContext.get().getContentResolver();
        ContentValues values = new ContentValues();
        values.put(MediaStore.Images.Media.DISPLAY_NAME, "copyclip_" + System.currentTimeMillis() + extensionForMime(mime));
        values.put(MediaStore.Images.Media.MIME_TYPE, mime);
        if (Build.VERSION.SDK_INT >= 29) {
            values.put(MediaStore.Images.Media.RELATIVE_PATH, "Pictures/CopyClip");
            values.put(MediaStore.Images.Media.IS_PENDING, 1);
        }
        Uri uri = resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values);
        if (uri == null) {
            throw new IllegalStateException("MediaStore insert returned null");
        }
        try (OutputStream os = resolver.openOutputStream(uri)) {
            if (os == null) {
                throw new IllegalStateException("openOutputStream returned null for " + uri);
            }
            os.write(data);
        }
        if (Build.VERSION.SDK_INT >= 29) {
            ContentValues done = new ContentValues();
            done.put(MediaStore.Images.Media.IS_PENDING, 0);
            resolver.update(uri, done, null, null);
        }
        return uri;
    }

    private static String extensionForMime(String mime) {
        switch (mime) {
            case "image/jpeg": return ".jpg";
            case "image/gif": return ".gif";
            case "image/bmp": return ".bmp";
            case "image/webp": return ".webp";
            default: return ".png";
        }
    }

    /** "<id> <mime> <sha256> <bytes> <nchunks>" -- header for a laptop-initiated chunked
     * image write. A fresh id discards any transfer already in progress. */
    private static void handleSetB(String rest) {
        String[] parts = rest.split(" ");
        if (parts.length < 5) {
            emit("ERR chunk ? malformed-setb");
            return;
        }
        String id = parts[0], mime = parts[1], sha = parts[2];
        long size;
        int nchunks;
        try {
            size = Long.parseLong(parts[3]);
            nchunks = Integer.parseInt(parts[4]);
        } catch (NumberFormatException e) {
            emit("ERR chunk " + id + " malformed-size");
            return;
        }
        if (size > MAX_IMAGE_BYTES) {
            emit("ERR chunk " + id + " oversize");
            inboundTransferId = null;
            return;
        }
        inboundTransferId = id;
        inboundMime = mime;
        inboundSha256 = sha;
        inboundTotalBytes = size;
        inboundNChunks = nchunks;
        inboundChunks.clear();
    }

    /** "<id> <idx> <b64chunk>" */
    private static void handleSetC(String rest) {
        int sp1 = rest.indexOf(' ');
        int sp2 = sp1 < 0 ? -1 : rest.indexOf(' ', sp1 + 1);
        if (sp1 < 0 || sp2 < 0) {
            emit("ERR chunk ? malformed-setc");
            return;
        }
        String id = rest.substring(0, sp1);
        String idxStr = rest.substring(sp1 + 1, sp2);
        String chunk = rest.substring(sp2 + 1);

        if (inboundTransferId == null || !inboundTransferId.equals(id)) {
            return; // unknown/superseded transfer; tolerated silently, matches the hub side
        }
        int idx;
        try {
            idx = Integer.parseInt(idxStr);
        } catch (NumberFormatException e) {
            emit("ERR chunk " + id + " bad-index");
            inboundTransferId = null;
            return;
        }
        if (idx != inboundChunks.size()) {
            emit("ERR chunk " + id + " gap");
            inboundTransferId = null;
            inboundChunks.clear();
            return;
        }
        inboundChunks.add(chunk);

        // ACK periodically so the sender's stall-detection deadline resets regardless of
        // image size -- see write()'s ACK_WINDOW handling in nodes/android_node.py. The
        // "< nchunks" guard avoids a redundant ACK right when the last chunk also happens
        // to land on a window boundary; the authoritative completion signal is the final
        // ACK set/DEFER set emitted once doSetImage() actually applies it.
        if (inboundChunks.size() % ACK_WINDOW == 0 && inboundChunks.size() < inboundNChunks) {
            emit("ACK setc " + id + " " + (inboundChunks.size() - 1));
        }

        if (inboundChunks.size() == inboundNChunks) {
            finishInboundTransfer(id);
        }
    }

    private static void finishInboundTransfer(String id) {
        try {
            StringBuilder b64 = new StringBuilder();
            for (String c : inboundChunks) {
                b64.append(c);
            }
            byte[] data = Base64.getDecoder().decode(b64.toString());
            String actualSha = sha256Bytes(data);
            if (!actualSha.equals(inboundSha256)) {
                emit("ERR chunk " + id + " sha-mismatch");
                return;
            }
            doSetImage(data, inboundMime);
        } catch (Throwable t) {
            emit("ERR chunk " + id + " " + describe(t));
        } finally {
            inboundTransferId = null;
            inboundChunks.clear();
        }
    }

    // ---------------------------------------------------------------- utils

    private static synchronized void emit(String line) {
        out.print(line);
        out.print('\n');
        out.flush();
    }

    /** Matches core/models.py's ClipPayload.sha256: hashes raw bytes, same digest used for
     * text (as UTF-8) and images alike so identity means the same thing on both sides of
     * the protocol. */
    private static String sha256Bytes(byte[] data) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] d = md.digest(data);
            StringBuilder sb = new StringBuilder(d.length * 2);
            for (byte b : d) {
                sb.append(Character.forDigit((b >> 4) & 0xf, 16));
                sb.append(Character.forDigit(b & 0xf, 16));
            }
            return sb.toString();
        } catch (Exception e) {
            return Integer.toHexString(Arrays.hashCode(data));
        }
    }

    private static String sha256(String s) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] d = md.digest(s.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(d.length * 2);
            for (byte b : d) {
                sb.append(Character.forDigit((b >> 4) & 0xf, 16));
                sb.append(Character.forDigit(b & 0xf, 16));
            }
            return sb.toString();
        } catch (Exception e) {
            return Integer.toHexString(s.hashCode());
        }
    }

    private static String describe(Throwable t) {
        if (t == null) {
            return "unknown";
        }
        String msg = t.getMessage();
        return t.getClass().getName() + (msg == null ? "" : ": " + msg);
    }

    private ClipBridge() {
    }
}
