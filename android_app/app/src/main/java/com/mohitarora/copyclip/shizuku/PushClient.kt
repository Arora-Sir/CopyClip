package com.mohitarora.copyclip.shizuku

import android.util.Log
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.InetSocketAddress
import java.net.Socket
import java.util.Base64

/**
 * Client role of transports/push_listener.py's handshake -- the one direction that runs
 * backwards from every other transport this app speaks: this device dials OUT to the hub
 * instead of the hub dialing in. Exists for "Send Clip": the caller (SendClipActivity) reads
 * the clipboard from an ordinary foreground Activity, the one clipboard-read path Android
 * grants with no special permission at all, confirmed against this project's own
 * ClipboardService log evidence during the pixel accessibility spike. Same Framing.kt
 * primitives BridgeServer.kt already uses for the bridge's own handshake, played from the
 * opposite role.
 */
object PushClient {

    sealed class Result {
        object Ok : Result()
        data class Failed(val reason: String) : Result()
    }

    fun push(
        host: String,
        port: Int,
        hmacKey: ByteArray,
        deviceName: String,
        mime: String,
        sensitive: Boolean,
        data: ByteArray,
    ): Result {
        if (host.isBlank()) return Result.Failed("no hub address configured")
        Log.i("CopyClip", "push: connecting to $host:$port as $deviceName ($mime, ${data.size}B)")
        // 20s, not 8s: an image push sends its whole payload as one line (no chunking, see
        // ARCHITECTURE.md's Send Clip section), a slow link needs more room than a
        // few-byte text push ever did.
        return try {
            Socket().use { socket ->
                socket.connect(InetSocketAddress(host, port), 8000)
                socket.soTimeout = 20_000
                val input = BufferedReader(InputStreamReader(socket.getInputStream(), Charsets.UTF_8))
                val output = socket.getOutputStream()

                fun writeLine(s: String) {
                    output.write((s + "\n").toByteArray(Charsets.UTF_8))
                    output.flush()
                }

                val hello = input.readLine() ?: return Result.Failed("no reply from hub")
                if (!hello.startsWith("HELLO ")) return Result.Failed("bad greeting: $hello")
                writeLine("AUTH ${Framing.challengeResponse(hmacKey, hello.removePrefix("HELLO "))}")

                val authReply = input.readLine() ?: return Result.Failed("connection closed after AUTH")
                if (authReply != "AUTH-OK") return Result.Failed("hub rejected our key")

                val challengeLine = input.readLine()
                    ?: return Result.Failed("connection closed before CHALLENGE")
                if (!challengeLine.startsWith("CHALLENGE ")) {
                    return Result.Failed("bad challenge: $challengeLine")
                }
                val theirNonce = challengeLine.removePrefix("CHALLENGE ")
                writeLine("RESPONSE ${Framing.challengeResponse(hmacKey, theirNonce)}")

                val b64 = Base64.getEncoder().encodeToString(data)
                val sensFlag = if (sensitive) "1" else "0"
                writeLine(Framing.seal(hmacKey, 1L, "PUSH $deviceName $mime $sensFlag $b64"))

                when (val reply = input.readLine()) {
                    null -> Result.Failed("no reply after push (wrong key on the hub side?)")
                    "PUSH-OK" -> Result.Ok
                    else -> Result.Failed(reply)
                }
            }.also { Log.i("CopyClip", "push: result = $it") }
        } catch (t: Throwable) {
            Log.e("CopyClip", "push: failed", t)
            Result.Failed("${t.javaClass.simpleName}: ${t.message}")
        }
    }
}
