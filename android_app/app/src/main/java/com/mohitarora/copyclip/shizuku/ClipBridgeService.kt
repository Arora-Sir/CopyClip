package com.mohitarora.copyclip.shizuku

import android.os.IBinder
import com.mohitarora.copyclip.IUserService
import java.io.File
import java.io.FileOutputStream
import java.lang.reflect.InvocationTargetException
import java.lang.reflect.Method
import kotlin.system.exitProcess

/**
 * Hosted by Shizuku as a UserService. `startBridge` spawns the exact, unmodified
 * `clipbridge.dex` and speaks the same HMAC-framed TCP protocol termux/copyclip_node.py
 * already proved out -- see BridgeServer.kt. `probe()` is the original spike diagnostic,
 * kept for troubleshooting.
 *
 * SHELL_UID guard is load-bearing, not defensive noise: Shizuku.bindUserService() succeeds
 * and hands back a perfectly working binder connection when Shizuku itself is running in
 * *root* mode too -- it just means this whole process, and everything startBridge() below
 * would spawn, runs as UID 0. Measured for real on a rooted-Shizuku device this session:
 * `ps -A` showed `root ... com.mohitarora.copyclip:service` after nothing but a normal
 * "Grant Shizuku access" + Save in the app's own UI, no explicit root request anywhere in
 * this codebase. This project's own standing rule is that ClipBridge never runs as root, for
 * any device, for any reason -- Shizuku granting root-level access does not create an
 * exception, so this checks the *actual* UID Shizuku handed this process and refuses to do
 * anything privileged unless it is exactly 2000 (shell), regardless of what the permission
 * grant itself reported as "success".
 */
class ClipBridgeService : IUserService.Stub() {

    companion object {
        private const val CALLING_PKG = "com.android.shell"
        private const val USER_ID = 0
        private const val DEVICE_ID = 0
        private const val SHELL_UID = 2000
        // /data/local/tmp is the traditional shell-writable, shell-readable shared temp
        // location (the same place `adb push` targets) -- a UserService process cannot
        // reliably assume it can read the app's own private/app-UID storage, since it "is
        // not a valid Android application process" per Shizuku's own doc comment.
        private const val DEX_PATH = "/data/local/tmp/copyclip_bridge.dex"
    }

    private var bridge: BridgeServer? = null
    private var lastRefusalReason: String? = null

    private fun requireShellUid(): Boolean {
        val uid = android.os.Process.myUid()
        if (uid != SHELL_UID) {
            lastRefusalReason = "refused: Shizuku granted uid=$uid, not shell (2000) -- " +
                "this app never runs privileged code as root, see ClipBridgeService.kt"
            android.util.Log.e("CopyClip", lastRefusalReason!!)
            return false
        }
        return true
    }

    override fun destroy() {
        bridge?.stop()
        exitProcess(0)
    }

    override fun startBridge(port: Int, hmacKey: ByteArray, dexBytes: ByteArray): Boolean {
        // Deliberately returns false here rather than exitProcess()-ing immediately: killing
        // the process mid-transaction would surface as a generic DeadObjectException on the
        // caller's side, losing lastRefusalReason entirely. Returning cleanly lets the caller
        // read the real reason via getBridgeStatus() and call destroy() itself -- no
        // privileged work (dex write, subprocess spawn, socket) happens either way, so the
        // extra few milliseconds this process stays alive changes nothing about what it does.
        if (!requireShellUid()) return false
        try {
            FileOutputStream(DEX_PATH).use { it.write(dexBytes) }
            File(DEX_PATH).setReadable(true, false)
        } catch (t: Throwable) {
            return false
        }
        bridge?.stop()
        val server = BridgeServer(port, hmacKey, DEX_PATH)
        val ok = server.start()
        if (ok) bridge = server
        return ok
    }

    override fun stopBridge() {
        bridge?.stop()
        bridge = null
    }

    override fun getBridgeStatus(): String =
        bridge?.status() ?: lastRefusalReason ?: "not started"

    override fun probe(): String {
        if (!requireShellUid()) return lastRefusalReason!!
        val out = StringBuilder()
        out.append("uid=").append(android.os.Process.myUid()).append('\n')

        val clipboard = try {
            reflectiveService("clipboard", "android.content.IClipboard\$Stub")
        } catch (t: Throwable) {
            out.append("service(clipboard) FAILED: ").append(t).append('\n')
            return out.toString()
        }
        out.append("service(clipboard) OK\n")

        try {
            val desc = reflectiveCall(clipboard, "getPrimaryClipDescription")
            out.append("getPrimaryClipDescription() OK: ").append(desc)
        } catch (t: Throwable) {
            out.append("getPrimaryClipDescription() FAILED: ").append(t)
        }
        return out.toString()
    }

    // -- reflection, mirrors android/ClipBridge.java's service()/call()/buildArgs() exactly --

    private fun reflectiveService(name: String, stubClass: String): Any {
        val sm = Class.forName("android.os.ServiceManager")
        val binder = sm.getMethod("getService", String::class.java)
            .invoke(null, name) as? IBinder
            ?: throw IllegalStateException("$name service not found")
        val stub = Class.forName(stubClass)
        return stub.getMethod("asInterface", IBinder::class.java).invoke(null, binder)!!
    }

    private fun reflectiveCall(target: Any, methodName: String): Any? {
        val candidates: List<Method> = target.javaClass.methods
            .filter { it.name == methodName }
            .sortedByDescending { it.parameterTypes.size }
        if (candidates.isEmpty()) throw NoSuchMethodException(methodName)

        var last: Throwable? = null
        for (m in candidates) {
            try {
                return m.invoke(target, *buildArgs(m))
            } catch (e: InvocationTargetException) {
                val cause = e.cause
                if (cause is SecurityException) throw cause
                last = cause ?: e
            } catch (t: Throwable) {
                last = t
            }
        }
        throw RuntimeException(
            "no usable overload for $methodName (${candidates.size} tried)", last
        )
    }

    private fun buildArgs(m: Method): Array<Any?> {
        var strSeen = 0
        var intSeen = 0
        return m.parameterTypes.map { t ->
            when {
                t == String::class.java -> if (strSeen++ == 0) CALLING_PKG else null
                t == Int::class.javaPrimitiveType -> if (intSeen++ == 0) USER_ID else DEVICE_ID
                t == Boolean::class.javaPrimitiveType -> false
                t == Long::class.javaPrimitiveType -> 0L
                t.isPrimitive -> 0
                else -> null
            }
        }.toTypedArray()
    }
}
