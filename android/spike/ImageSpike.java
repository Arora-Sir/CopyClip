package com.mohitarora.copyclip.spike;

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
import android.content.pm.ApplicationInfo;
import android.net.Uri;
import android.os.Binder;
import android.os.Build;
import android.os.IBinder;
import android.os.Looper;
import android.os.Process;
import android.provider.MediaStore;

import java.io.InputStream;
import java.io.OutputStream;
import java.io.PrintStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Comparator;
import java.util.List;

/**
 * THROWAWAY device spike for image clipboard sync. Not shipped, not referenced by
 * ClipBridge.java. Exists only to answer the 9 measured questions in the image-sync plan
 * (docs/bugs/SPIKE-IMAGE-CLIPBOARD.md) before any production code is written.
 *
 * Ported from otherCopyProjects/scrcpy: the ActivityThread/FakeContext trick
 * (Workarounds.java, FakeContext.java) is used ONLY to obtain a working ContentResolver
 * under app_process. Clipboard reads/writes reuse ClipBridge.java's own proven reflective
 * IClipboard-binder approach, not FakeContext's getSystemService(CLIPBOARD_SERVICE) -- per
 * the plan's decision to keep the working text path untouched.
 */
public final class ImageSpike {

    private static final String CALLING_PKG = "com.android.shell";
    private static final int USER_ID = 0;
    private static final int DEVICE_ID = 0;

    // 1x1 red pixel PNG, 67 bytes. Embedded so the spike needs no file transfer beyond the dex.
    private static final byte[] TEST_PNG = Base64.getDecoder().decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==");

    private static PrintStream out;

    public static void main(String[] args) {
        out = new PrintStream(new java.io.FileOutputStream(java.io.FileDescriptor.out), true,
                StandardCharsets.UTF_8);
        // ActivityThread's constructor builds a Handler, which throws unless this thread has
        // a prepared Looper. app_process's main thread has none by default -- ClipBridge.java
        // never needed one because it talks to service binders directly, but the
        // ActivityThread/FakeContext trick does.
        if (Looper.myLooper() == null) {
            Looper.prepareMainLooper();
        }
        String mode = args.length > 0 ? args[0] : "full";
        try {
            switch (mode) {
                case "islocked": cmdIsLocked(); break;
                case "context": cmdContext(); break;
                case "heap": cmdHeap(); break;
                case "insert": cmdInsert(); break;
                case "clipwrite": cmdClipWrite(args.length > 1 ? args[1] : null); break;
                case "clipread": cmdClipRead(); break;
                case "full": cmdFull(); break;
                default:
                    emit("ERR unknown mode " + mode);
                    System.exit(2);
            }
        } catch (Throwable t) {
            emit("ERR " + mode + ": " + describe(t));
            t.printStackTrace(out);
            System.exit(1);
        }
        System.exit(0);
    }

    // ------------------------------------------------------------ Q8: lock state (no imaging)

    private static void cmdIsLocked() throws Exception {
        Object trust = service("trust", "android.app.trust.ITrustManager$Stub");
        Object r = call(trust, "isDeviceLocked", null);
        emit("LOCKED " + r);
    }

    // ------------------------------------------------------------ Q9: heap headroom

    private static void cmdHeap() {
        int mb = 24;
        byte[] buf = new byte[mb * 1024 * 1024];
        for (int i = 0; i < buf.length; i += 4096) {
            buf[i] = 1; // touch every page so it's actually committed, not just reserved
        }
        emit("HEAP_OK " + mb + "MB allocated and touched");
    }

    // ------------------------------------------------------------ Q1: FakeContext / ContentResolver

    private static void cmdContext() throws Exception {
        ContentResolver resolver = FakeContext.get().getContentResolver();
        emit("CONTEXT_OK resolver=" + resolver.getClass().getName());
    }

    // ------------------------------------------------------------ Q2/Q3: MediaStore insert + write

    private static Uri insertTestImage() throws Exception {
        ContentResolver resolver = FakeContext.get().getContentResolver();
        ContentValues values = new ContentValues();
        values.put(MediaStore.Images.Media.DISPLAY_NAME, "copyclip_spike_" + System.currentTimeMillis() + ".png");
        values.put(MediaStore.Images.Media.MIME_TYPE, "image/png");
        if (Build.VERSION.SDK_INT >= 29) {
            values.put(MediaStore.Images.Media.RELATIVE_PATH, "Pictures/CopyClip");
            values.put(MediaStore.Images.Media.IS_PENDING, 1);
        }
        Uri collection = MediaStore.Images.Media.EXTERNAL_CONTENT_URI;
        Uri uri = resolver.insert(collection, values);
        if (uri == null) {
            throw new IllegalStateException("resolver.insert returned null");
        }
        try (OutputStream os = resolver.openOutputStream(uri)) {
            if (os == null) {
                throw new IllegalStateException("openOutputStream returned null for " + uri);
            }
            os.write(TEST_PNG);
        }
        if (Build.VERSION.SDK_INT >= 29) {
            ContentValues done = new ContentValues();
            done.put(MediaStore.Images.Media.IS_PENDING, 0);
            resolver.update(uri, done, null, null);
        }
        return uri;
    }

    private static void cmdInsert() throws Exception {
        Uri uri = insertTestImage();
        emit("INSERT_OK " + uri);
    }

    // ------------------------------------------------------------ Q4/Q5: clip write via real IClipboard

    private static void cmdClipWrite(String uriArg) throws Exception {
        Uri uri = uriArg != null ? Uri.parse(uriArg) : insertTestImage();
        Object clipboard = service("clipboard", "android.content.IClipboard$Stub");

        ClipDescription desc = new ClipDescription("CopyClip-spike", new String[]{"image/png"});
        ClipData clip = new ClipData(desc, new ClipData.Item(uri));

        call(clipboard, "setPrimaryClip", clip);

        // Verify the same way ClipBridge.doSet() does: read the description straight back,
        // because a locked device accepts setPrimaryClip and silently discards it.
        Object after = call(clipboard, "getPrimaryClipDescription", null);
        emit("CLIPWRITE_OK uri=" + uri + " readback=" + (after != null ? after.toString() : "null(likely locked/discarded)"));
    }

    // ------------------------------------------------------------ Q6/Q7: clip read + cross-app URI

    private static void cmdClipRead() throws Exception {
        Object clipboard = service("clipboard", "android.content.IClipboard$Stub");
        Object clipObj = call(clipboard, "getPrimaryClip", null);
        if (clipObj == null) {
            emit("CLIPREAD_EMPTY (null -- likely locked, or nothing on clipboard)");
            return;
        }
        ClipData clip = (ClipData) clipObj;
        if (clip.getItemCount() == 0) {
            emit("CLIPREAD_EMPTY (zero items)");
            return;
        }
        ClipDescription desc = clip.getDescription();
        String mime = desc != null && desc.getMimeTypeCount() > 0 ? desc.getMimeType(0) : "unknown";
        ClipData.Item item = clip.getItemAt(0);
        Uri uri = item.getUri();
        emit("CLIPREAD mime=" + mime + " uri=" + uri + " text=" + item.getText());
        if (uri == null) {
            return;
        }
        ContentResolver resolver = FakeContext.get().getContentResolver();
        try (InputStream is = resolver.openInputStream(uri)) {
            if (is == null) {
                emit("CLIPREAD_OPEN_NULL for " + uri);
                return;
            }
            byte[] buf = readAll(is);
            emit("CLIPREAD_BYTES " + buf.length + " sha256=" + sha256(buf));
        } catch (SecurityException se) {
            emit("CLIPREAD_SECURITY_EXCEPTION " + se.getMessage());
        } catch (Exception e) {
            emit("CLIPREAD_READ_FAILED " + describe(e));
        }
    }

    // ------------------------------------------------------------ full sequence

    private static void cmdFull() throws Exception {
        cmdIsLocked();
        cmdHeap();
        cmdContext();
        Uri uri = insertTestImage();
        emit("INSERT_OK " + uri);
        cmdClipWrite(uri.toString());
        cmdClipRead();
        emit("FULL_DONE");
    }

    // ---------------------------------------------------------------- FakeContext (scrcpy port)

    /**
     * Ported from otherCopyProjects/scrcpy FakeContext.java + Workarounds.java. Used ONLY to
     * obtain a working ContentResolver -- the clipboard itself is never touched through this
     * class, exactly per the plan's decision to leave the proven IClipboard-binder path alone.
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
            // jar -- android.content.ContentResolver#acquireProvider etc. are @hide. Verbatim
            // pattern from otherCopyProjects/scrcpy FakeContext.java.)
            protected IContentProvider acquireProvider(Context c, String name) {
                return SpikeActivityManager.get().getContentProviderExternal(name, new Binder());
            }
            // @Override (but super-class method not visible)
            public boolean releaseProvider(IContentProvider icp) { return false; }
            // @Override (but super-class method not visible)
            //
            // scrcpy's own FakeContext always returns null here, because scrcpy never does
            // file I/O through this resolver. That is NOT safe to copy verbatim: measured on
            // the S24 Ultra, ContentResolver#openAssetFileDescriptor calls
            // acquireUnstableProvider() FIRST and throws "No content provider: <uri>"
            // immediately on a null return, rather than falling back to acquireProvider().
            // openOutputStream/openInputStream -- exactly what image sync needs -- go through
            // that path, so this must delegate for real.
            protected IContentProvider acquireUnstableProvider(Context c, String name) {
                return SpikeActivityManager.get().getContentProviderExternal(name, new Binder());
            }
            // @Override (but super-class method not visible)
            public boolean releaseUnstableProvider(IContentProvider icp) { return false; }
            // @Override (but super-class method not visible)
            public void unstableProviderDied(IContentProvider icp) { /* ignore */ }
        };

        private FakeContext() {
            super(getSystemContext());
        }

        @Override public String getPackageName() { return PACKAGE_NAME; }
        @Override public String getOpPackageName() { return PACKAGE_NAME; }

        @Override
        public AttributionSource getAttributionSource() {
            if (Build.VERSION.SDK_INT < 31) {
                return null;
            }
            AttributionSource.Builder b = new AttributionSource.Builder(Process.SHELL_UID);
            b.setPackageName(PACKAGE_NAME);
            return b.build();
        }

        @Override public Context getApplicationContext() { return this; }
        @Override public Context createPackageContext(String pkg, int flags) { return this; }
        @Override public ContentResolver getContentResolver() { return contentResolver; }

        private static void fillConfigurationController() {
            // "On some Samsung devices, DisplayManagerGlobal.getDisplayInfoLocked() calls
            // ActivityThread.currentActivityThread().getConfiguration(), which requires a
            // non-null ConfigurationController." -- scrcpy issue #4467. Our S24 Ultra / Tab
            // S10 Ultra are exactly this class of device, hence attempting it here even
            // though we only need getSystemContext(), not video/audio mirroring.
            try {
                Class<?> ccClass = Class.forName("android.app.ConfigurationController");
                Class<?> atInternal = Class.forName("android.app.ActivityThreadInternal");
                Constructor<?> ctor = ccClass.getDeclaredConstructor(atInternal);
                ctor.setAccessible(true);
                Object cc = ctor.newInstance(ACTIVITY_THREAD);
                Field f = ACTIVITY_THREAD_CLASS.getDeclaredField("mConfigurationController");
                f.setAccessible(true);
                f.set(ACTIVITY_THREAD, cc);
            } catch (Throwable t) {
                emit("WARN fillConfigurationController failed (may be harmless): " + describe(t));
            }
        }

        private static void fillAppContext() {
            try {
                Application app = Instrumentation.newApplication(Application.class, FakeContext.get());
                Field f = ACTIVITY_THREAD_CLASS.getDeclaredField("mInitialApplication");
                f.setAccessible(true);
                f.set(ACTIVITY_THREAD, app);
            } catch (Throwable t) {
                emit("WARN fillAppContext failed (may be harmless): " + describe(t));
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

    /** Minimal port of scrcpy's ActivityManager wrapper: only getContentProviderExternal. */
    private static final class SpikeActivityManager {
        private static SpikeActivityManager instance;
        private final Object manager;
        private Method getContentProviderExternalMethod;
        private boolean newVersion = true;

        static synchronized SpikeActivityManager get() {
            if (instance == null) {
                try {
                    Class<?> cls = Class.forName("android.app.ActivityManagerNative");
                    Method getDefault = cls.getDeclaredMethod("getDefault");
                    Object am = getDefault.invoke(null);
                    instance = new SpikeActivityManager(am);
                } catch (Exception e) {
                    throw new AssertionError(e);
                }
            }
            return instance;
        }

        private SpikeActivityManager(Object manager) {
            this.manager = manager;
        }

        IContentProvider getContentProviderExternal(String name, IBinder token) {
            try {
                if (getContentProviderExternalMethod == null) {
                    try {
                        getContentProviderExternalMethod = manager.getClass().getMethod(
                            "getContentProviderExternal", String.class, int.class, IBinder.class, String.class);
                    } catch (NoSuchMethodException e) {
                        getContentProviderExternalMethod = manager.getClass().getMethod(
                            "getContentProviderExternal", String.class, int.class, IBinder.class);
                        newVersion = false;
                    }
                }
                Object[] args = newVersion
                    ? new Object[]{name, FakeContext.ROOT_UID, token, null}
                    : new Object[]{name, FakeContext.ROOT_UID, token};
                Object holder = getContentProviderExternalMethod.invoke(manager, args);
                if (holder == null) {
                    return null;
                }
                Field providerField = holder.getClass().getDeclaredField("provider");
                providerField.setAccessible(true);
                return (IContentProvider) providerField.get(holder);
            } catch (Exception e) {
                emit("WARN getContentProviderExternal(" + name + ") failed: " + describe(e));
                return null;
            }
        }
    }

    // ---------------------------------------------------------------- binder helpers (from ClipBridge.java)

    private static Object service(String name, String stubClass) throws Exception {
        Class<?> sm = Class.forName("android.os.ServiceManager");
        IBinder binder = (IBinder) sm.getMethod("getService", String.class).invoke(null, name);
        if (binder == null) {
            throw new IllegalStateException(name + " service not found");
        }
        Class<?> stub = Class.forName(stubClass);
        return stub.getMethod("asInterface", IBinder.class).invoke(null, binder);
    }

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
                if (cause instanceof SecurityException) {
                    throw (SecurityException) cause;
                }
                last = cause != null ? cause : e;
            } catch (Throwable t) {
                last = t;
            }
        }
        throw new RuntimeException("no usable overload for " + name + ": " + describe(last));
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

    // ---------------------------------------------------------------- utils

    private static byte[] readAll(InputStream is) throws Exception {
        java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = is.read(buf)) != -1) {
            bos.write(buf, 0, n);
        }
        return bos.toByteArray();
    }

    private static String sha256(byte[] data) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        byte[] d = md.digest(data);
        StringBuilder sb = new StringBuilder(d.length * 2);
        for (byte b : d) {
            sb.append(Character.forDigit((b >> 4) & 0xf, 16));
            sb.append(Character.forDigit(b & 0xf, 16));
        }
        return sb.toString();
    }

    private static synchronized void emit(String line) {
        out.print(line);
        out.print('\n');
        out.flush();
    }

    private static String describe(Throwable t) {
        if (t == null) {
            return "unknown";
        }
        String msg = t.getMessage();
        return t.getClass().getName() + (msg == null ? "" : ": " + msg);
    }

    private ImageSpike() {
    }
}
