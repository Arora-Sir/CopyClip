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
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.PrintStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.security.SecureRandom;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
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
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import org.json.JSONObject;

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
 * image to the clipboard (a ClipData.Item needs a real content:// URI: a file:// URI
 * throws FileUriExposedException), which under app_process needs the FakeContext trick
 * (ported from scrcpy, see docs/bugs/SPIKE-IMAGE-CLIPBOARD.md for what was measured and the two
 * bugs found and fixed in that port). Text's setPrimaryClip/getPrimaryClip continue to go
 * through the plain reflective IClipboard binder above: FakeContext is scoped narrowly to
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
    private static final long POLL_MS_LOCKED = 1500L;

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
     * When true this device announces sensitive clips (OTPs, Authenticator codes) rather
     * than dropping them with SKIP. Set via CONF handshake from the laptop.
     */
    private static volatile boolean allowSensitive = false;

    private static volatile boolean pendingSetSensitive = false;

    private static final ScheduledExecutorService wipeExecutor = Executors.newSingleThreadScheduledExecutor();
    private static volatile ScheduledFuture<?> pendingWipeFuture;
    private static volatile long sensitiveWipeSeconds = 10L;

    private static volatile String hubHost = null;
    private static volatile int pushPort = 8387;
    private static volatile byte[] keyBytes = null;
    private static volatile String deviceNodeName = "phone";

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
     * still in full mode, keeps reading content: see the orphan check in serve() below.
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
     * regardless of image size: see nodes/android_node.py's ACK_WINDOW for the full
     * rationale; the two constants must agree for the pairing to land where the sender
     * expects it, though a mismatch would only cost a slightly early/late reset, not
     * correctness. */
    private static final int ACK_WINDOW = 8;

    /** Independent, device-side enforcement of the same cap core/policy.py applies on the
     * laptop: belt and braces: a compromised or buggy laptop-side check must not be the
     * only thing standing between a malicious/huge SETB header and unbounded memory use
     * here. */
    private static long MAX_IMAGE_BYTES = 62_914_560L; // 60 MiB

    /** Pass through byte-for-byte, no re-encode. Anything else (avif/unknown)
     * is transcoded to PNG: see readImageBytes(). */
    private static final Set<String> PASSTHROUGH_MIMES = new HashSet<>(Arrays.asList(
            "image/png", "image/jpeg", "image/gif", "image/bmp", "image/webp", "image/heic", "image/heif"));

    private static final AtomicLong OUTBOUND_TRANSFER_SEQ = new AtomicLong();

    /** True once FakeContext's ContentResolver has been proven to construct without
     * throwing. Guards every image code path below; when false, images are skipped
     * (reads) or rejected (writes) but text sync is completely unaffected: see
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
     * whichever thread later handles GET: these can be different threads. */
    private static volatile byte[] cachedImageBytes;
    private static volatile String cachedImageMime;
    private static volatile String cachedImageHash;
    private static volatile long cachedImageTs = Long.MIN_VALUE;

    /** Reassembly state for a laptop-initiated chunked image (SETB/SETC). Touched only
     * from the stdin-reader thread (readCommands()'s single loop), so: like the rest of
     * that thread's command-handling state: it needs no locking. */
    private static String inboundTransferId;
    private static String inboundMime;
    private static String inboundSha256;
    private static long inboundTotalBytes;
    private static int inboundNChunks;
    private static final List<String> inboundChunks = new ArrayList<>();

    public static void main(String[] args) throws java.io.UnsupportedEncodingException {
        // PrintStream(OutputStream, boolean, Charset) is Java 10+ only: NoSuchMethodError
        // on Android 10 (SDK 29)'s core-oj.jar, measured on a real Pixel (sailfish). The
        // String-encoding-name overload below has existed since Android API 1 and behaves
        // identically; "UTF-8" can never actually trigger this checked exception, every
        // JVM/ART implementation is required to support it.
        out = new PrintStream(new java.io.FileOutputStream(java.io.FileDescriptor.out), true,
                "UTF-8");
        try {
            clipboard = connectClipboard();
        } catch (Throwable t) {
            emit("ERR connect: " + describe(t));
            System.exit(1);
            return;
        }
        initImaging(); // failure here must never affect text sync: see its own try/catch

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
                case "sets":
                    if (args.length < 2) {
                        emit("ERR sets requires a base64 argument");
                        System.exit(2);
                        return;
                    }
                    doSetSensitive(args[1]);
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
     * code path trusts it. Any failure here: and there is real reason to expect one on
     * some OEM builds, see Shizuku-API's own warning quoted in docs/bugs/SPIKE-IMAGE-CLIPBOARD.md
     *: disables imaging and falls back to text-only, exactly as if this were an older
     * ClipBridge build. Never allowed to affect connectClipboard()'s already-working state.
     */
    private static void initImaging() {
        try {
            if (Looper.myLooper() == null) {
                // ActivityThread's constructor (inside FakeContext's static init) builds a
                // Handler, which throws unless this thread has a prepared Looper.
                // app_process's main thread has none by default. Measured directly:
                // docs/bugs/SPIKE-IMAGE-CLIPBOARD.md.
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

    private static synchronized void cancelSensitiveWipe() {
        if (pendingWipeFuture != null) {
            pendingWipeFuture.cancel(false);
            pendingWipeFuture = null;
        }
    }

    private static synchronized void scheduleSensitiveWipe() {
        cancelSensitiveWipe();
        final long targetStamp = ownWriteStamp;
        pendingWipeFuture = wipeExecutor.schedule(() -> {
            try {
                ClipDescription current = getDescription();
                long currentStamp = current == null ? 0L : current.getTimestamp();
                if (currentStamp == targetStamp) {
                    // Clipboard has not changed since the sensitive write. Wipe it.
                    // Re-using doSet("") ensures suppressHash and ownWriteStamp are updated,
                    // so this wipe write emits STAMP instead of echoing DESC to other nodes.
                    String emptyB64 = Base64.getEncoder().encodeToString("".getBytes(StandardCharsets.UTF_8));
                    doSetInternal(emptyB64, false);
                }
            } catch (Throwable ignored) {
            }
        }, sensitiveWipeSeconds, TimeUnit.SECONDS);
    }

    /**
     * Writes the clipboard, or parks the payload if the device is locked.
     *
     * The write is verified by reading the description straight back, because a locked device
     * accepts setPrimaryClip and discards it without raising anything. Trusting the absent
     * exception is exactly how this fails silently.
     */
    private static void doSet(String b64) throws Exception {
        doSetInternal(b64, false);
    }

    private static void doSetSensitive(String b64) throws Exception {
        doSetInternal(b64, true);
    }

    private static void doSetInternal(String b64, boolean sensitive) throws Exception {
        if (isLocked()) {
            pendingSet = b64;
            pendingSetSensitive = sensitive;
            emit("DEFER set locked");
            return;
        }
        // A non-sensitive write supersedes any pending sensitive-wipe timer.
        if (!sensitive) {
            cancelSensitiveWipe();
        }
        String text = new String(Base64.getDecoder().decode(b64), StandardCharsets.UTF_8);
        suppressHash = sha256(text);
        // Note: ClipData.newPlainText() does not accept PersistableBundle extras. On the write
        // direction to Android, we write plain text. System UI protections are honored on the
        // read direction (EXTRA_IS_SENSITIVE), while write direction relies on the 60s auto-wipe.
        call(clipboard, "setPrimaryClip", ClipData.newPlainText("CopyClip", text));

        ClipDescription after = getDescription();
        if (after == null) {
            pendingSet = b64;
            pendingSetSensitive = sensitive;
            emit("DEFER set discarded");
            return;
        }
        // Remember the stamp our own write produced so the poll loop does not read it back.
        ownWriteStamp = after.getTimestamp();
        pendingSet = null;
        pendingSetSensitive = false;
        if (sensitive) {
            scheduleSensitiveWipe();
        }
        emit("ACK set");
    }


    /** Replays a parked write once the device unlocks. */
    private static void flushPending() {
        String b64 = pendingSet;
        boolean sens = pendingSetSensitive;
        if (b64 != null && !isLocked()) {
            try {
                pendingSet = null;
                pendingSetSensitive = false;
                if (sens) {
                    doSetSensitive(b64);
                } else {
                    doSet(b64);
                }
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
        // A ClipDescription can declare several representations of the same item, and
        // Android does not order them "plain text first": e.g. the system Sharesheet's
        // built-in "Copy" target on a URL declares ["text/x-moz-url", "text/plain"], first
        // slot first. getMimeType(0) alone reported that non-standard first slot, which
        // getText() (used unconditionally below and in emitCurrentClip()) resolves to the
        // clean URL regardless: but core/policy.py's TEXT_MIMES only recognises the
        // literal strings "text/plain"/"text/html", so a perfectly good link was reported
        // under a mime the laptop had never heard of and silently rejected as non-text. See
        // docs/bugs/BUG-007-long-link-copy-rejected-as-non-text.md.
        if (desc.hasMimeType(ClipDescription.MIMETYPE_TEXT_PLAIN)) {
            return ClipDescription.MIMETYPE_TEXT_PLAIN;
        }
        if (desc.hasMimeType(ClipDescription.MIMETYPE_TEXT_HTML)) {
            return ClipDescription.MIMETYPE_TEXT_HTML;
        }
        // Scan for image MIME types before falling back to slot 0: Android content providers
        // often declare ["application/octet-stream", "image/png"] or multiple types. If any
        // slot is an image, prioritize the image MIME so it is not dropped as non-text.
        for (int i = 0; i < desc.getMimeTypeCount(); i++) {
            String m = desc.getMimeType(i);
            if (m != null && m.toLowerCase(Locale.ROOT).startsWith("image/")) {
                return m;
            }
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

        if (isSensitive(desc) && !allowSensitive) {
            emit("SKIP " + mime + " sensitive");
            return;
        }
        if (isImageMime(mime)) {
            if (System.currentTimeMillis() - lastScreenshotEmitMs < IMAGE_DESC_HOLD_MS) {
                return; // a real screenshot just went out; this is almost certainly its
                        // spurious clipboard-preview sibling (see IMAGE_DESC_HOLD_MS above)
            }
            // Held rather than announced immediately: checkScreenshots() runs every poll
            // tick and may still supersede this in the next tick or two if it turns out to
            // be that same spurious sibling arriving *before* the real file (see the main
            // loop's resolution of pendingImageDesc).
            pendingImageDesc = desc;
            pendingImageDescSinceMs = System.currentTimeMillis();
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
        emit("DESC " + mime + " " + (isSensitive(desc) ? 1 : 0) + " "
                + (desc == null ? 0L : desc.getTimestamp())
                + " " + hash + " " + text.length());
    }

    /** Returns the sha256 of what was emitted, or null when nothing was emitted. */
    private static String emitCurrentClip(boolean force) throws Exception {
        ClipDescription desc = getDescription();
        String mime = mimeOf(desc);

        if (isSensitive(desc) && !allowSensitive) {
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
        loadPersistedConf();
        initScreenshotWatch();
        initCameraWatch();
        initInboundStreamServer();

        Thread stdin = new Thread(ClipBridge::readCommands, "copyclip-stdin");
        stdin.setDaemon(true);
        stdin.start();

        long lastStamp = -1L;
        boolean lastLocked = !isLocked(); // force an initial LOCK report

        while (true) {
            try {
                boolean locked = isLocked();
                // Pass locked state to watchers. When locked in a pocket, scans back off to 60s
                // and camera uses pure inode mtime to eliminate idle CPU cycles and heap allocations.
                checkScreenshots(locked);
                checkCameraPhotos(locked);

                // Resolve a held image DESC (see emitDesc()): checkScreenshots() above just
                // ran, so if it turned out to be the real file for this same screenshot,
                // lastScreenshotEmitMs has already moved past pendingImageDescSinceMs and
                // this candidate is dropped, never announced to the laptop at all. Otherwise
                // it is genuinely unrelated to any screenshot and is released once the hold
                // window passes.
                if (pendingImageDesc != null) {
                    if (lastScreenshotEmitMs >= pendingImageDescSinceMs) {
                        pendingImageDesc = null;
                    } else if (System.currentTimeMillis() - pendingImageDescSinceMs
                            >= IMAGE_DESC_HOLD_MS) {
                        if (!locked) {
                            ClipDescription held = pendingImageDesc;
                            pendingImageDesc = null;
                            emitImageDesc(held);
                        }
                    }
                }

                if (locked != lastLocked) {
                    lastLocked = locked;
                    emit("LOCK " + (locked ? 1 : 0));
                    if (!locked) {
                        // Unlocking makes the clipboard readable again and invalidates the
                        // "empty" we saw while locked, so resync from scratch.
                        lastStamp = -1L;
                        lastScreenshotScanMs = 0L;
                        lastCameraPhotoScanMs = 0L;
                        flushPending();
                    }
                }
                if (locked) {
                    Thread.sleep(POLL_MS_LOCKED);
                    continue; // reads return null while locked; nothing useful to do
                }

                ClipDescription desc = getDescription();
                // Polling the description is cheap: it never transfers clip content and
                // never raises a read notification. Only a changed timestamp justifies
                // looking at the clip at all.
                long stamp = desc == null ? 0L : desc.getTimestamp();
                if (stamp != lastStamp) {
                    lastStamp = stamp;
                    boolean isSens = isSensitive(desc) && allowSensitive;
                    if (stamp == ownWriteStamp) {
                        // Our own write echoing back. Reading it would tell us nothing we
                        // do not already know, and would raise a "Shell pasted from your
                        // clipboard" toast for a clip the user never copied.
                        emit("STAMP " + stamp);
                    } else if (stampOnly && !isSens) {
                        // User made a local copy while in stamp-only mode. Sensitive clips
                        // bypass this because Samsung Continuity never mirrors them.
                        cancelSensitiveWipe();
                        emit("STAMP " + stamp);
                    } else if (System.currentTimeMillis() - lastHubContact > IDLE_ORPHAN_MS) {
                        // Orphaned: the laptop has been silent well past its own idle-ping
                        // cycle, most likely because the network dropped and this process
                        // just hasn't been torn down yet. Treat like stampOnly until contact
                        // resumes, rather than reading content nobody can currently receive.
                        cancelSensitiveWipe();
                        emit("STAMP " + stamp);
                    } else {
                        // User made a local copy. Cancel stale sensitive-wipe before reading.
                        cancelSensitiveWipe();
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
                        case "SETS":
                            doSetSensitive(rest);
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
                        case "CONF":
                            handleConf(rest);
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

    private static void loadPersistedConf() {
        try {
            File confFile = new File("/data/local/tmp/copyclip_conf.json");
            if (confFile.exists() && confFile.length() > 0) {
                byte[] raw = readFileBounded(confFile, 65536);
                if (raw != null && raw.length > 0) {
                    applyConfJson(new String(raw, StandardCharsets.UTF_8));
                }
            }
        } catch (Throwable t) {
            emit("DEBUG conf-load: " + describe(t));
        }
    }

    private static void applyConfJson(String jsonStr) {
        try {
            JSONObject obj = new JSONObject(jsonStr);
            if (obj.has("allow_sensitive")) {
                allowSensitive = obj.optBoolean("allow_sensitive", false);
            }
            if (obj.has("sensitive_wipe_s")) {
                sensitiveWipeSeconds = Math.max(1L, obj.optLong("sensitive_wipe_s", 10L));
            } else if (obj.has("wipe_s")) {
                sensitiveWipeSeconds = Math.max(1L, obj.optLong("wipe_s", 10L));
            }
            if (obj.has("camera_backup")) {
                boolean cb = obj.optBoolean("camera_backup", true);
                if (cb && !watchCamera && lastCameraPhotoName == null) {
                    initCameraWatch();
                }
                watchCamera = cb;
            }
            if (obj.has("max_image_bytes")) {
                MAX_IMAGE_BYTES = obj.optLong("max_image_bytes", 62_914_560L);
            }
            if (obj.has("hub_host")) {
                hubHost = obj.optString("hub_host", null);
            }
            if (obj.has("push_port")) {
                pushPort = obj.optInt("push_port", 8387);
            }
            if (obj.has("device_name")) {
                deviceNodeName = obj.optString("device_name", deviceNodeName);
            }
            if (obj.has("key_hex")) {
                String kh = obj.optString("key_hex", "").trim();
                if (!kh.isEmpty()) {
                    keyBytes = kh.getBytes(StandardCharsets.UTF_8);
                }
            }
        } catch (Throwable ignored) {
        }
    }

    private static void handleConf(String rest) {
        try {
            byte[] decoded = Base64.getDecoder().decode(rest);
            File confFile = new File("/data/local/tmp/copyclip_conf.json");
            try (FileOutputStream fos = new FileOutputStream(confFile)) {
                fos.write(decoded);
            }
            confFile.setReadable(true, false);
            confFile.setWritable(true, false);

            applyConfJson(new String(decoded, StandardCharsets.UTF_8));
            emit("CONF-ACK");
            if (inboundStreamPort > 0) {
                emit("STREAM-PORT " + inboundStreamPort);
            }
        } catch (Throwable t) {
            emit("ERR conf: " + describe(t));
        }
    }

    // ---------------------------------------------------------------- imaging (FakeContext)

    /**
     * Ported from otherCopyProjects/scrcpy FakeContext.java + Workarounds.java, scoped
     * narrowly to obtaining a working ContentResolver under app_process: the clipboard
     * itself is never touched through this class; setPrimaryClip/getPrimaryClip stay on the
     * proven reflective IClipboard-binder path above.
     *
     * Two fixes beyond a straight port, both found and measured on this project's own
     * hardware (docs/bugs/SPIKE-IMAGE-CLIPBOARD.md), neither of which scrcpy itself needed
     * because it never does file I/O through this resolver:
     *   1. initImaging() prepares a main Looper first: ActivityThread's constructor
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
            // jar: android.content.ContentResolver#acquireProvider etc. are @hide.)
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
            // content provider: <uri>" immediately on a null return: it does not fall
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
            // non-null ConfigurationController.": scrcpy issue #4467. Both our target
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
     * without holding a per-URI grant: the shell UID's own permission is the check, not
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
     * decoder: BitmapFactory/Bitmap need no Context and no hidden API, unlike everything
     * else in this file.
     *
     * Size-gated before reading: OpenableColumns.SIZE is checked first when the provider
     * supports it, and readAllBounded() enforces the cap on the actual byte count either
     * way, so a misreporting or SIZE-less provider still cannot exhaust memory.
     *
     * Returns {byte[] data, String mime}. Throws on any failure: callers must treat that
     * as "skip this image", per docs/bugs/SPIKE-IMAGE-CLIPBOARD.md's Q5/Q7 finding that reading
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

    // Read file with single preallocated buffer to avoid dynamic ByteArrayOutputStream reallocation.
    private static byte[] readFileBounded(File file, long cap) throws Exception {
        long len = file.length();
        if (len > cap) {
            throw new IllegalStateException("file length exceeds cap (" + len + " > " + cap + ")");
        }
        int size = (int) len;
        byte[] data = new byte[size];
        try (InputStream is = new FileInputStream(file)) {
            int offset = 0;
            while (offset < size) {
                int read = is.read(data, offset, size - offset);
                if (read == -1) {
                    break;
                }
                offset += read;
            }
            if (offset != size) {
                return Arrays.copyOf(data, offset);
            }
            return data;
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
     * Samsung's screenshot capture does not call setPrimaryClip(): measured directly:
     * docs/bugs/SCREENSHOT-SYNC-FIX.md, three real screenshots taken while this poll loop was
     * running produced zero clipboard events. The "clipboard panel" showing a recent
     * screenshot is a convenience suggestion list, not the actual system clipboard. So
     * there is no clip event to catch here; instead this watches the screenshot folder
     * directly for new files, using the exact same poll-every-tick approach already used
     * for clipboard timestamps: no FileObserver, no hidden API, consistent with the rest
     * of this file's philosophy.
     *
     * Confirmed via `adb shell` on this hardware: /sdcard/DCIM/Screenshots (NOT
     * Pictures/Screenshots, which does not exist on this device), identical path on both
     * phone and tablet, and the shell UID has full read access to both the listing and
     * file contents via its sdcard_rw/sdcard_r group membership: no ContentResolver, no
     * FakeContext needed, plain java.io.File/FileInputStream.
     */
    private static final File[] SCREENSHOT_DIRS = new File[] {
        new File("/sdcard/DCIM/Screenshots"),
        new File("/sdcard/Pictures/Screenshots"),
    };

    private static final long[] lastDirMtimes = new long[SCREENSHOT_DIRS.length];
    private static File cachedNewestScreenshot = null;
    private static long lastScreenshotScanMs = 0L;

    private static final File[] CAMERA_DIRS = new File[] {
        new File("/sdcard/DCIM/Camera"),
        new File("/sdcard/DCIM/100MEDIA"),
    };
    private static final long[] lastCameraDirMtimes = new long[CAMERA_DIRS.length];
    private static File cachedNewestCameraPhoto = null;
    private static String lastCameraPhotoName;
    private static long lastCameraPhotoScanMs = 0L;
    private static long lastCameraFirstSyncMs = 0L;
    private static long lastCameraLastSyncedLen = -1L;
    private static boolean watchCamera = true;

    private static String lastScreenshotName;
    // BUG-014: when lastScreenshotName was first synced (ms) and how many bytes were last
    // observed for it, so a same-filename overwrite (Samsung's scroll capture finalizes a
    // long screenshot by rewriting the SAME path with the full stitched image, seconds after
    // the initial single-page shot, measured directly on this hardware, see
    // docs/bugs/BUG-014-samsung-long-screenshot-scroll-capture-partial-sync.md) can still be
    // caught within a bounded follow-up window, instead of that filename being ignored
    // forever once first seen.
    private static long lastScreenshotFirstSyncMs = 0L;
    private static long lastScreenshotLastSyncedLen = -1L;
    // Measured directly on an S24 Ultra (BUG-014): the same-filename overwrite from a
    // single-page shot to the final stitched long screenshot landed 8s and 22s after the
    // initial shot, across two real captures. Set with margin above that. Anchored to the
    // FIRST sync of a filename and never renewed by a later re-check, so the total extra
    // watch time per screenshot is capped regardless of how many times the user extends it.
    private static final long SCREENSHOT_REARM_WINDOW_MS = 45_000L;
    // How many consecutive polls must see the same file length before
    // readCompleteScreenshotFile() trusts a positive isCompleteImage() verdict.
    private static final int SCREENSHOT_STABILITY_TICKS = 1;

    // Recently-pushed screenshot hashes, so a rare case where Samsung's own screenshot flow
    // produces two on-disk files for one real capture (different filenames: e.g. a
    // scrolling-screenshot preview-then-final pair) doesn't get pushed twice. Defense in
    // depth on top of checkScreenshots()'s filename-identity check above: that check
    // handles the *measured*, common case (same file rewritten in place by a background
    // process: see docs/bugs/BUG-005-duplicate-screenshot-push.md); this handles a
    // same-content-different-filename case that check structurally can't catch. Not a
    // substitute for suppressHash below: that's a single slot for the device's own most
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

    // Coordinates checkScreenshots() (the file watcher) with the ordinary clipboard-image
    // path (emitDesc()/emitImageDesc()): see docs/bugs/BUG-006-duplicate-screenshot-clipboard-
    // preview.md. Measured on this device: taking a screenshot reliably produces the real
    // file AND, separately, a second, much smaller image on the ordinary clipboard, 0-2s
    // apart in either order: traced to a companion app's own screenshot-notification
    // handling (a small preview bitmap from the system "Screenshot captured" notification),
    // not anything CopyClip does. The two never share a content hash, so the existing
    // hash-based dedups above cannot tell them apart, and neither can a laptop-side size
    // comparison: that would also collapse two genuinely different images a user
    // deliberately copies close together, which core/dedup.py's own test suite explicitly
    // protects (a different image must always get its own sync, regardless of timing).
    // This coordinates from the one place that actually knows which path an image came
    // from, instead of guessing from content on the other end of the wire.
    private static long lastScreenshotEmitMs = 0L;
    private static ClipDescription pendingImageDesc;
    private static long pendingImageDescSinceMs;
    // Comfortably above the largest gap measured between the real file and the spurious
    // second clipboard image (~3.5s on high-resolution 2960x1788 devices like Tab S10 Ultra);
    // short enough that a genuine standalone image copy is barely delayed. Known, accepted
    // trade-off: two genuinely different images copied deliberately less than this far apart
    // on the SAME device, with no screenshot involved at all, would have the first one
    // superseded: judged unlikely relative to how often a screenshot is actually taken.
    private static final long IMAGE_DESC_HOLD_MS = 4_500;

    /**
     * Establishes the starting baseline silently: called once before the poll loop
     * starts, so the newest screenshot already on disk when ClipBridge launches is never
     * announced as if it just appeared. This is deliberately a *local* baseline, not the
     * hub's existing on_node_connected() baseline mechanism: that mechanism is single-shot
     * per node (discards itself after the first clip it sees), so a device that can emit
     * two independent "first" signals: current clipboard content, and current newest
     * screenshot: would have the second one wrongly treated as a genuine new clip to
     * sync. Keeping this baseline entirely on the device side avoids that collision.
     */
    private static void initScreenshotWatch() {
        File newest = findNewestScreenshot();
        if (newest != null) {
            long ageMs = System.currentTimeMillis() - newest.lastModified();
            // If the newest screenshot was captured very recently (< 90s ago), do NOT
            // adopt it as a silent baseline. Leaving lastScreenshotName null allows
            // checkScreenshots() to emit it on the very first poll tick, preventing
            // screenshots taken during a reconnect or app start from being lost.
            if (ageMs >= 90_000L) {
                lastScreenshotName = newest.getName();
                // BUG-014: seed the re-arm tracking so this silently-adopted baseline is
                // immediately treated as past its window (0 is always more than
                // SCREENSHOT_REARM_WINDOW_MS ms in the past) and never re-checked, same as
                // this baseline was never treated as "new" before this fix.
                lastScreenshotFirstSyncMs = 0L;
                lastScreenshotLastSyncedLen = newest.length();
            }
        }
    }

    private static File findNewestScreenshot() {
        return findNewestScreenshot(false);
    }

    private static File findNewestScreenshot(boolean locked) {
        long now = System.currentTimeMillis();
        boolean anyDirChanged = false;
        for (int i = 0; i < SCREENSHOT_DIRS.length; i++) {
            File dir = SCREENSHOT_DIRS[i];
            long mtime = dir.lastModified();
            if (mtime != lastDirMtimes[i]) {
                anyDirChanged = true;
                lastDirMtimes[i] = mtime;
            }
        }
        // Inode mtime check is primary and consumes under 0.05ms of CPU.
        // Fallback interval provides an edge-case safety net: 10s while unlocked,
        // 60s while locked in pocket. Zero Dalvik heap churn during normal idle.
        long interval = locked ? 60_000L : 1_000L;
        boolean scanDue = anyDirChanged || (now - lastScreenshotScanMs >= interval) || cachedNewestScreenshot == null;
        if (!scanDue) {
            return cachedNewestScreenshot;
        }
        lastScreenshotScanMs = now;

        File newest = null;
        long newestTime = -1L;
        for (File dir : SCREENSHOT_DIRS) {
            File[] files = dir.listFiles();
            if (files == null || files.length == 0) {
                continue;
            }
            for (File f : files) {
                if (!f.isFile()) {
                    continue;
                }
                long m = f.lastModified();
                if (newest == null || m > newestTime) {
                    newest = f;
                    newestTime = m;
                }
            }
        }
        cachedNewestScreenshot = newest;
        return newest;
    }

    private static void checkScreenshots() {
        checkScreenshots(false);
    }

    private static void checkScreenshots(boolean locked) {
        try {
            File newest = findNewestScreenshot(locked);
            if (newest == null) {
                return;
            }
            long ageMs = System.currentTimeMillis() - newest.lastModified();
            if (ageMs > 120_000L && lastScreenshotName != null && !newest.getName().equals(lastScreenshotName)) {
                lastScreenshotName = newest.getName();
                lastScreenshotFirstSyncMs = System.currentTimeMillis();
                lastScreenshotLastSyncedLen = newest.length();
                return;
            }
            boolean sameName = lastScreenshotName != null && newest.getName().equals(lastScreenshotName);
            if (sameName) {
                // BUG-014: a synced filename stays worth re-checking for a bounded window
                // after its first sync, since Samsung's scroll capture finalizes a long
                // screenshot by overwriting the SAME path with the full stitched image,
                // seconds after the initial single-page shot (measured directly, see
                // docs/bugs/BUG-014-samsung-long-screenshot-scroll-capture-partial-sync.md).
                // Gated on length, never on mtime alone, so BUG-005's fix (a background
                // process re-touching identical bytes must never re-trigger a resync) still
                // holds inside this window.
                long now = System.currentTimeMillis();
                if (now - lastScreenshotFirstSyncMs > SCREENSHOT_REARM_WINDOW_MS) {
                    return; // window elapsed, filename fully retired, same as before this fix
                }
                if (newest.length() == lastScreenshotLastSyncedLen) {
                    return; // no growth since last look, nothing to do
                }
                // Falls through: same filename, still inside the window, and it grew, so
                // re-run it through the same read-and-send path as a fresh file below.
            }
            // Read only when file write is 100% complete. If still writing, do NOT advance
            // lastScreenshotName so the next poll will pick it up as soon as it finishes.
            byte[] raw = readCompleteScreenshotFile(newest);
            if (raw == null || raw.length <= 0) {
                // If it timed out after 8s and still is not a valid complete image,
                // advance lastScreenshotName so we don't spin in an infinite 8-second
                // blocking loop on every poll tick, starving the main thread. Only seed the
                // re-arm window on a genuinely new filename, never on a re-check of an
                // existing one, so the window can't be extended indefinitely.
                lastScreenshotName = newest.getName();
                if (!sameName) {
                    lastScreenshotFirstSyncMs = System.currentTimeMillis();
                }
                lastScreenshotLastSyncedLen = newest.length();
                emit("SKIP image/unknown screenshot-incomplete-or-unsupported: " + newest.getName());
                return;
            }
            if (!sameName) {
                lastScreenshotFirstSyncMs = System.currentTimeMillis();
            }
            lastScreenshotName = newest.getName();
            lastScreenshotLastSyncedLen = raw.length;
            emitScreenshot(raw, newest.getName());
        } catch (Throwable t) {
            emit("ERR screenshot: " + describe(t));
        }
    }

    private static void initCameraWatch() {
        File newest = findNewestCameraPhoto();
        if (newest != null) {
            long ageMs = System.currentTimeMillis() - newest.lastModified();
            if (ageMs >= 90_000L) {
                lastCameraPhotoName = newest.getName();
                lastCameraFirstSyncMs = 0L;
                lastCameraLastSyncedLen = newest.length();
            }
        }
    }

    private static File findNewestCameraPhoto() {
        return findNewestCameraPhoto(false);
    }

    private static File findNewestCameraPhoto(boolean locked) {
        long now = System.currentTimeMillis();
        boolean anyDirChanged = false;
        for (int i = 0; i < CAMERA_DIRS.length; i++) {
            File dir = CAMERA_DIRS[i];
            long mtime = dir.lastModified();
            if (mtime != lastCameraDirMtimes[i]) {
                anyDirChanged = true;
                lastCameraDirMtimes[i] = mtime;
            }
        }
        // Camera directory often contains thousands of photos.
        // Strictly require directory mtime change to eliminate Dalvik heap allocations.
        boolean scanDue = anyDirChanged || cachedNewestCameraPhoto == null;
        if (!scanDue) {
            return cachedNewestCameraPhoto;
        }
        lastCameraPhotoScanMs = now;

        File newest = null;
        long newestTime = -1L;
        for (File dir : CAMERA_DIRS) {
            File[] files = dir.listFiles();
            if (files == null || files.length == 0) {
                continue;
            }
            for (File f : files) {
                if (!f.isFile()) {
                    continue;
                }
                String name = f.getName().toLowerCase(Locale.ROOT);
                if (!name.endsWith(".jpg") && !name.endsWith(".jpeg") && !name.endsWith(".png")
                        && !name.endsWith(".heic") && !name.endsWith(".heif")) {
                    continue;
                }
                long m = f.lastModified();
                if (newest == null || m > newestTime) {
                    newest = f;
                    newestTime = m;
                }
            }
        }
        cachedNewestCameraPhoto = newest;
        return newest;
    }

    private static void checkCameraPhotos() {
        checkCameraPhotos(false);
    }

    private static void checkCameraPhotos(boolean locked) {
        if (!watchCamera) {
            return;
        }
        try {
            File newest = findNewestCameraPhoto(locked);
            if (newest == null) {
                return;
            }
            long ageMs = System.currentTimeMillis() - newest.lastModified();
            if (ageMs > 120_000L && lastCameraPhotoName != null && !newest.getName().equals(lastCameraPhotoName)) {
                lastCameraPhotoName = newest.getName();
                lastCameraFirstSyncMs = 0L;
                lastCameraLastSyncedLen = newest.length();
                return;
            }
            boolean sameName = lastCameraPhotoName != null && newest.getName().equals(lastCameraPhotoName);
            if (sameName) {
                long now = System.currentTimeMillis();
                if (now - lastCameraFirstSyncMs > SCREENSHOT_REARM_WINDOW_MS) {
                    return;
                }
                if (newest.length() == lastCameraLastSyncedLen) {
                    return;
                }
            }
            byte[] raw = readCompleteScreenshotFile(newest);
            if (raw == null || raw.length <= 0) {
                return;
            }
            if (!sameName) {
                lastCameraFirstSyncMs = System.currentTimeMillis();
            }
            lastCameraPhotoName = newest.getName();
            lastCameraLastSyncedLen = raw.length;
            emitCameraPhoto(raw, newest.getName());
        } catch (Throwable t) {
            emit("ERR camera: " + describe(t));
        }
    }

    private static void emitCameraPhoto(byte[] raw, String filename) {
        if (raw == null || raw.length <= 0) {
            return;
        }
        if (raw.length > MAX_IMAGE_BYTES) {
            emit("SKIP image/unknown camera-oversize");
            return;
        }
        String mime = sniffImageMime(raw);
        String hash = sha256Bytes(raw);
        if (hash.equals(suppressHash)) {
            return;
        }
        if (recentlyPushedScreenshot(hash)) {
            return;
        }
        if (tryDirectTcpStream(raw, mime, false, hash, true)) {
            emit("ACK stream-cam " + hash);
            return;
        }
        // Camera transfers use transfer id prefix "cam-" to signal file-only backup.
        // Stream chunks directly from raw slices to maintain minimal Dalvik heap overhead.
        String id = "cam-" + OUTBOUND_TRANSFER_SEQ.incrementAndGet();
        int nchunks = Math.max(1, (raw.length + CHUNK_BYTES - 1) / CHUNK_BYTES);
        emit("CLIPB " + id + " " + mime + " 0 " + hash + " " + raw.length + " " + nchunks);
        for (int idx = 0, offset = 0; offset < raw.length; idx++, offset += CHUNK_BYTES) {
            int len = Math.min(CHUNK_BYTES, raw.length - offset);
            byte[] slice = Arrays.copyOfRange(raw, offset, offset + len);
            String chunkB64 = Base64.getEncoder().encodeToString(slice);
            emit("CLIPC " + id + " " + idx + " " + chunkB64);
        }
    }

    private static boolean isCompleteImage(byte[] data) {
        if (data == null || data.length < 12) return false;
        int len = data.length;

        // PNG: must contain IEND chunk
        if ((data[0] & 0xFF) == 0x89 && data[1] == 'P' && data[2] == 'N' && data[3] == 'G') {
            // Standard PNG ends with 12-byte IEND chunk (00 00 00 00 49 45 4E 44 AE 42 60 82)
            if (len >= 12 && data[len - 8] == 'I' && data[len - 7] == 'E' && data[len - 6] == 'N' && data[len - 5] == 'D') {
                return true;
            }
            // In case of ancillary chunks / trailing bytes, scan backwards up to 4096 bytes for IEND
            int scanLimit = Math.max(8, len - 4096);
            for (int i = len - 8; i >= scanLimit; i--) {
                if (data[i] == 'I' && data[i + 1] == 'E' && data[i + 2] == 'N' && data[i + 3] == 'D') {
                    return true;
                }
            }
            return false;
        }

        // JPEG: starts with SOI (FF D8) and must contain EOI (FF D9)
        if ((data[0] & 0xFF) == 0xFF && (data[1] & 0xFF) == 0xD8) {
            // 1. Standard JPEG: ends with FF D9
            if (len >= 2 && (data[len - 2] & 0xFF) == 0xFF && (data[len - 1] & 0xFF) == 0xD9) {
                return true;
            }
            // 2. Samsung SEF trailer: ends with 'SEFT' (0x53 0x45 0x46 0x54)
            if (len >= 4 && data[len - 4] == 'S' && data[len - 3] == 'E' && data[len - 2] == 'F' && data[len - 1] == 'T') {
                return true;
            }
            // 3. Trailing metadata / padding: scan backwards from end (up to 64KB) for FF D9
            int scanLimit = Math.max(2, len - 65536);
            for (int i = len - 2; i >= scanLimit; i--) {
                if ((data[i] & 0xFF) == 0xFF && (data[i + 1] & 0xFF) == 0xD9) {
                    return true;
                }
            }
            return false;
        }

        // GIF: must end with 0x3B (trailer)
        if (data[0] == 'G' && data[1] == 'I' && data[2] == 'F') {
            if (data[len - 1] == 0x3B) return true;
            int scanLimit = Math.max(6, len - 512);
            for (int i = len - 1; i >= scanLimit; i--) {
                if (data[i] == 0x3B) return true;
            }
            return false;
        }

        // BMP: header specifies file size at offset 2 (little endian)
        if (data[0] == 'B' && data[1] == 'M' && len >= 6) {
            long bmpSize = (data[2] & 0xFFL) | ((data[3] & 0xFFL) << 8) | ((data[4] & 0xFFL) << 16) | ((data[5] & 0xFFL) << 24);
            return len >= bmpSize && bmpSize > 0;
        }

        // WebP: RIFF header specifies file size at offset 4 (little endian)
        if (len >= 12 && data[0] == 'R' && data[1] == 'I' && data[2] == 'F' && data[3] == 'F' &&
            data[8] == 'W' && data[9] == 'E' && data[10] == 'B' && data[11] == 'P') {
            long riffSize = (data[4] & 0xFFL) | ((data[5] & 0xFFL) << 8) | ((data[6] & 0xFFL) << 16) | ((data[7] & 0xFFL) << 24);
            return len >= (riffSize + 8) && riffSize > 0;
        }

        // HEIC / HEIF / ISOBMFF: starts with ftyp box at offset 4
        if (len >= 12 && data[4] == 'f' && data[5] == 't' && data[6] == 'y' && data[7] == 'p') {
            return len >= 1024;
        }

        return true;
    }

    private static byte[] readCompleteScreenshotFile(File file) {
        long deadline = System.currentTimeMillis() + 8000L; // Allow up to 8s for slow flash write
        long previousLen = -1L;
        int stableTicks = 0;
        while (System.currentTimeMillis() < deadline) {
            long currentLen = file.length();
            if (currentLen > 100) {
                stableTicks = (currentLen == previousLen) ? stableTicks + 1 : 1;
                previousLen = currentLen;
                // BUG-014: require the length to also be unchanged since the previous poll
                // before trusting isCompleteImage()'s verdict. A file still being written
                // can, purely by chance, have currently-written trailing bytes that look
                // like a valid terminator; requiring one quiet tick first means that
                // coincidence would have to survive a second read to be accepted.
                if (stableTicks >= SCREENSHOT_STABILITY_TICKS) {
                    try {
                        byte[] raw = readFileBounded(file, MAX_IMAGE_BYTES);
                        if (isCompleteImage(raw)) {
                            return raw;
                        }
                    } catch (Exception ignored) {
                    }
                }
            } else {
                previousLen = -1L;
                stableTicks = 0;
            }
            try {
                Thread.sleep(80);
            } catch (InterruptedException ignored) {
            }
        }
        // Final attempt strictly verified against image completion
        try {
            byte[] raw = readFileBounded(file, MAX_IMAGE_BYTES);
            if (isCompleteImage(raw)) {
                return raw;
            }
        } catch (Exception ignored) {
        }
        return null; // Never push an incomplete/half-baked file
    }

    /**
     * Reads a newly-detected screenshot file and routes it through chunked CLIPB/CLIPC.
     */
    private static void emitScreenshot(byte[] raw, String filename) {
        if (raw == null || raw.length <= 0) {
            emit("SKIP image/unknown screenshot-read-failed-or-empty");
            return;
        }
        if (raw.length > MAX_IMAGE_BYTES) {
            emit("SKIP image/unknown screenshot-oversize");
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
        // A held or about-to-arrive ordinary-clipboard image (see emitDesc()) is very
        // likely this same real screenshot surfacing a second time, not a deliberate
        // separate copy: the real file always wins, so drop the candidate outright.
        lastScreenshotEmitMs = System.currentTimeMillis();
        pendingImageDesc = null;
        sendChunkedClip(outMime, false, hash, data);
    }

    /**
     * Detects format from magic bytes rather than assuming JPEG (the observed format for
     * Samsung's own screenshots): a screenshot tool or Android version could plausibly
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
        if (data.length >= 12 && data[4] == 'f' && data[5] == 't' && data[6] == 'y' && data[7] == 'p') {
            return "image/heic";
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

    /** Returns the sha256 emitted, or null when nothing was emitted: mirrors
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
            // What emitImageDesc() already read and hashed for this exact clip: a GET
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
        if (tryDirectTcpStream(data, mime, sensitive, hash, false)) {
            emit("ACK stream " + hash);
            return;
        }
        String id = "dev-" + OUTBOUND_TRANSFER_SEQ.incrementAndGet();
        int nchunks = Math.max(1, (data.length + CHUNK_BYTES - 1) / CHUNK_BYTES);
        emit("CLIPB " + id + " " + mime + " " + (sensitive ? 1 : 0) + " " + hash + " "
                + data.length + " " + nchunks);
        for (int idx = 0, offset = 0; offset < data.length; idx++, offset += CHUNK_BYTES) {
            int len = Math.min(CHUNK_BYTES, data.length - offset);
            byte[] slice = Arrays.copyOfRange(data, offset, offset + len);
            String chunkB64 = Base64.getEncoder().encodeToString(slice);
            emit("CLIPC " + id + " " + idx + " " + chunkB64);
        }
    }

    // ------------------------------------------------------------ image write (laptop -> device)

    /**
     * Applies an already-reassembled, sha-verified image to the clipboard, or parks it if
     * the device is locked. Mirrors doSet()'s structure exactly: the write is verified by
     * reading the description straight back, because a locked device accepts
     * setPrimaryClip and discards it without raising anything: the same silent-discard
     * behaviour measured for text (NOTES.md) and re-confirmed for images
     * (docs/bugs/SPIKE-IMAGE-CLIPBOARD.md).
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
        // An image write supersedes any pending sensitive-wipe timer.
        cancelSensitiveWipe();

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
     * READ_MEDIA_IMAGES can paste it: maximum compatibility, per the implementation
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

    /** "<id> <mime> <sha256> <bytes> <nchunks>": header for a laptop-initiated chunked
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
        // image size: see write()'s ACK_WINDOW handling in nodes/android_node.py. The
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
            byte[] data = new byte[(int) inboundTotalBytes];
            int offset = 0;
            for (String c : inboundChunks) {
                byte[] decodedChunk = Base64.getDecoder().decode(c);
                System.arraycopy(decodedChunk, 0, data, offset, decodedChunk.length);
                offset += decodedChunk.length;
            }
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

    private static String bytesToHex(byte[] bytes) {
        StringBuilder sb = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) {
            sb.append(Character.forDigit((b >> 4) & 0xf, 16));
            sb.append(Character.forDigit(b & 0xf, 16));
        }
        return sb.toString();
    }

    private static byte[] hexStringToByteArray(String s) {
        int len = s.length();
        byte[] data = new byte[len / 2];
        for (int i = 0; i < len; i += 2) {
            data[i / 2] = (byte) ((Character.digit(s.charAt(i), 16) << 4)
                                 + Character.digit(s.charAt(i + 1), 16));
        }
        return data;
    }

    private static String hmacSha256(byte[] key, byte[] data) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        SecretKeySpec secretKey = new SecretKeySpec(key, "HmacSHA256");
        mac.init(secretKey);
        byte[] hmacBytes = mac.doFinal(data);
        return bytesToHex(hmacBytes);
    }

    private static String sealFrame(byte[] key, int seq, String payload) throws Exception {
        String msg = seq + ":" + payload;
        String mac = hmacSha256(key, msg.getBytes(StandardCharsets.UTF_8));
        return seq + " " + mac + " " + payload;
    }

    private static boolean tryDirectTcpStream(byte[] raw, String mime, boolean sensitive,
                                             String hash, boolean isCamera) {
        String host = hubHost;
        int port = pushPort;
        byte[] key = keyBytes;
        String nodeName = deviceNodeName;
        if (host == null || host.isEmpty() || key == null || key.length == 0) {
            return false;
        }
        Socket socket = null;
        try {
            socket = new Socket();
            socket.setTcpNoDelay(true);
            socket.setSendBufferSize(524288);
            socket.connect(new InetSocketAddress(host, port), 4000);
            socket.setSoTimeout(60000);

            BufferedReader reader = new BufferedReader(new InputStreamReader(
                    socket.getInputStream(), StandardCharsets.UTF_8));
            OutputStream out = socket.getOutputStream();

            String helloLine = reader.readLine();
            if (helloLine == null || !helloLine.startsWith("HELLO ")) {
                return false;
            }
            String srvNonce = helloLine.substring(6).trim();

            String authResp = hmacSha256(key, srvNonce.getBytes(StandardCharsets.UTF_8));
            out.write(("AUTH " + authResp + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();

            String authOk = reader.readLine();
            if (authOk == null || !authOk.equals("AUTH-OK")) {
                return false;
            }

            String challLine = reader.readLine();
            if (challLine == null || !challLine.startsWith("CHALLENGE ")) {
                return false;
            }
            String myNonce = challLine.substring(10).trim();

            String challResp = hmacSha256(key, myNonce.getBytes(StandardCharsets.UTF_8));
            out.write(("RESPONSE " + challResp + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();

            String payloadLine = "STREAM " + nodeName + " " + mime + " "
                    + (sensitive ? "1" : "0") + " " + (isCamera ? "1" : "0") + " "
                    + hash + " " + raw.length;
            int seq = 1;
            String frame = sealFrame(key, seq, payloadLine);
            out.write((frame + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();

            out.write(raw);
            out.flush();

            String streamResp = reader.readLine();
            if (streamResp != null && streamResp.equals("STREAM-OK")) {
                return true;
            }
            return false;
        } catch (Throwable t) {
            return false;
        } finally {
            if (socket != null) {
                try {
                    socket.close();
                } catch (Throwable ignored) {
                }
            }
        }
    }

    // ---------------------------------------------------------------- inbound stream server (reverse direct streaming)

    private static ServerSocket inboundStreamServer = null;
    private static int inboundStreamPort = 0;
    private static final SecureRandom RNG = new SecureRandom();

    private static String newNonce() {
        byte[] b = new byte[16];
        RNG.nextBytes(b);
        return bytesToHex(b);
    }

    private static void initInboundStreamServer() {
        try {
            inboundStreamServer = new ServerSocket(0);
            inboundStreamPort = inboundStreamServer.getLocalPort();
            emit("STREAM-PORT " + inboundStreamPort);
            Thread t = new Thread(ClipBridge::inboundStreamAcceptLoop, "copyclip-inbound-stream");
            t.setDaemon(true);
            t.start();
        } catch (Throwable t) {
            emit("DEBUG stream-server-init: " + describe(t));
        }
    }

    private static void inboundStreamAcceptLoop() {
        while (inboundStreamServer != null && !inboundStreamServer.isClosed()) {
            try {
                Socket sock = inboundStreamServer.accept();
                new Thread(() -> handleInboundStreamConnection(sock), "copyclip-stream-worker").start();
            } catch (Throwable t) {
                if (inboundStreamServer == null || inboundStreamServer.isClosed()) {
                    return;
                }
            }
        }
    }

    private static String readAsciiLine(InputStream in) throws Exception {
        ByteArrayOutputStream baos = new ByteArrayOutputStream(128);
        int b;
        while ((b = in.read()) != -1) {
            if (b == '\n') {
                break;
            }
            if (b != '\r') {
                baos.write(b);
            }
        }
        if (b == -1 && baos.size() == 0) {
            return null;
        }
        return baos.toString("UTF-8");
    }

    private static void handleInboundStreamConnection(Socket sock) {
        try {
            sock.setSoTimeout(60000);
            sock.setTcpNoDelay(true);
            sock.setReceiveBufferSize(524288);
            InputStream in = sock.getInputStream();
            OutputStream out = sock.getOutputStream();

            String nonce = newNonce();
            out.write(("HELLO " + nonce + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();

            String authLine = readAsciiLine(in);
            byte[] key = keyBytes;
            if (key == null || authLine == null) {
                out.write("AUTH-FAIL\n".getBytes(StandardCharsets.UTF_8));
                return;
            }
            String expectedAuth = "AUTH " + hmacSha256(key, nonce.getBytes(StandardCharsets.UTF_8));
            if (!authLine.trim().equals(expectedAuth)) {
                out.write("AUTH-FAIL\n".getBytes(StandardCharsets.UTF_8));
                return;
            }
            out.write("AUTH-OK\n".getBytes(StandardCharsets.UTF_8));
            out.flush();

            String myNonce = newNonce();
            out.write(("CHALLENGE " + myNonce + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();

            String respLine = readAsciiLine(in);
            if (respLine == null || !respLine.startsWith("RESPONSE ")) {
                return;
            }
            String expectedResp = "RESPONSE " + hmacSha256(key, myNonce.getBytes(StandardCharsets.UTF_8));
            if (!respLine.trim().equals(expectedResp)) {
                return;
            }

            String frame = readAsciiLine(in);
            if (frame == null) {
                return;
            }
            String[] frameParts = frame.trim().split(" ", 3);
            if (frameParts.length < 3) {
                return;
            }
            int seq = Integer.parseInt(frameParts[0]);
            String mac = frameParts[1];
            String payloadLine = frameParts[2];
            String expectedMac = hmacSha256(key, (seq + ":" + payloadLine).getBytes(StandardCharsets.UTF_8));
            if (!mac.equalsIgnoreCase(expectedMac)) {
                out.write("STREAM-FAIL auth\n".getBytes(StandardCharsets.UTF_8));
                return;
            }

            // payloadLine: "STREAM <node> <mime> <sens> <sha256> <total_bytes>"
            String[] parts = payloadLine.split(" ");
            if (parts.length < 6 || !parts[0].equals("STREAM")) {
                out.write("STREAM-FAIL malformed\n".getBytes(StandardCharsets.UTF_8));
                return;
            }
            String mime = parts[2];
            String expectedSha = parts[4];
            int totalBytes = Integer.parseInt(parts[5]);

            if (totalBytes > MAX_IMAGE_BYTES) {
                out.write("STREAM-FAIL oversize\n".getBytes(StandardCharsets.UTF_8));
                return;
            }

            byte[] buf = new byte[totalBytes];
            int read = 0;
            while (read < totalBytes) {
                int n = in.read(buf, read, totalBytes - read);
                if (n < 0) {
                    out.write("STREAM-FAIL truncated\n".getBytes(StandardCharsets.UTF_8));
                    return;
                }
                read += n;
            }

            String actualSha = sha256Bytes(buf);
            if (!actualSha.equalsIgnoreCase(expectedSha)) {
                out.write("STREAM-FAIL sha_mismatch\n".getBytes(StandardCharsets.UTF_8));
                return;
            }

            out.write("STREAM-OK\n".getBytes(StandardCharsets.UTF_8));
            out.flush();

            doSetImage(buf, mime);
        } catch (Throwable t) {
            emit("DEBUG inbound-stream-err: " + describe(t));
        } finally {
            try {
                sock.close();
            } catch (Throwable ignored) {
            }
        }
    }

    private ClipBridge() {
    }
}
