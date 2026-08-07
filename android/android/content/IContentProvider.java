package android.content;

public interface IContentProvider {
    // android.content.IContentProvider is hidden; this is a fake one to expose the type to
    // the project. At runtime the boot classloader supplies the real interface, so the
    // anonymous ContentResolver.acquireProvider override in ClipBridge.java's FakeContext
    // binds by dex descriptor. Verbatim copy of scrcpy's stub
    // (server/src/main/java/android/content/IContentProvider.java in otherCopyProjects) --
    // see otherCopyProjects/NOTES.md's corrected FakeContext section and
    // bugs/SPIKE-IMAGE-CLIPBOARD.md for why this is the only hidden-API surface needed, and
    // why it compiles fine against the stock platforms/android-36/android.jar already used
    // by build_dex.ps1 -- no patched hidden-API jar required.
}
