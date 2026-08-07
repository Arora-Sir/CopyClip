package android.content;

public interface IContentProvider {
    // android.content.IContentProvider is hidden; this is a fake one to expose the type to
    // the project. At runtime the boot classloader supplies the real interface, so the
    // anonymous ContentResolver.acquireProvider override in FakeContext binds by dex
    // descriptor. Verbatim copy of scrcpy's stub
    // (server/src/main/java/android/content/IContentProvider.java) -- see Finding A in the
    // image-sync plan: this is the ONLY hidden-API surface needed, and it compiles fine
    // against the stock platforms/android-36/android.jar already used by build_dex.ps1.
}
