package me.mohit.copyclip;

import android.content.ClipData;
import android.content.ClipDescription;
import android.os.IBinder;
import android.os.PersistableBundle;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Comparator;
import java.util.List;

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
 *   in:  SET <base64-utf8>
 *        GET          (send the body)
 *        DESC         (send metadata only)
 *        MODE stamp   (announce timestamps only, never read content)
 *        MODE full    (read content to produce DESC hashes)
 *        PING
 *        QUIT
 *
 * The poll loop announces changes with DESC, never CLIP. The laptop asks for the body with
 * GET only when it does not already recognise the hash, which stops Samsung's
 * phone-to-tablet mirroring from putting every clip on the wire twice.
 *
 * Reading clip *content* raises Android's "Shell pasted from your clipboard" toast, so
 * content reads are minimised aggressively: never for our own writes, and never at all on
 * a standby device whose Samsung sibling is already being read.
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
        if (b64 == null || isLocked()) {
            return;
        }
        try {
            pendingSet = null;
            doSet(b64);
        } catch (Throwable t) {
            emit("ERR flush: " + describe(t));
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
        if (desc != null && !desc.hasMimeType(ClipDescription.MIMETYPE_TEXT_PLAIN)
                && !desc.hasMimeType(ClipDescription.MIMETYPE_TEXT_HTML)) {
            // v1 is text only. Images and files land here until the MediaStore path is built.
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

        Thread stdin = new Thread(ClipBridge::readCommands, "copyclip-stdin");
        stdin.setDaemon(true);
        stdin.start();

        long lastStamp = -1L;
        boolean lastLocked = !isLocked(); // force an initial LOCK report

        while (true) {
            try {
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

    // ---------------------------------------------------------------- utils

    private static synchronized void emit(String line) {
        out.print(line);
        out.print('\n');
        out.flush();
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
