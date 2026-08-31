package com.mohitarora.copyclip.shizuku

import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.atomic.AtomicReference

/**
 * Kotlin port of termux/copyclip_node.py, running inside a Shizuku UserService (shell UID)
 * instead of Termux. Same wire protocol (Framing.kt mirrors transports/framing.py exactly),
 * same one-client-at-a-time model, same trick of spawning the *unmodified* clipbridge.dex
 * as a subprocess and piping its stdio over the authenticated socket rather than
 * reimplementing any clipboard-access logic here. That logic already works (text and
 * images both) and has its own history of hard-won fixes (BUG-009, SPIKE-IMAGE-CLIPBOARD) --
 * porting it a second time in Kotlin would mean re-earning every one of those fixes blind.
 */
class BridgeServer(
    private val port: Int,
    private val hmacKey: ByteArray,
    private val dexPath: String,
) {
    @Volatile private var serverSocket: ServerSocket? = null
    @Volatile private var running = false
    private var currentProc: Process? = null
    private val statusRef = AtomicReference("stopped")

    fun status(): String = statusRef.get()

    fun start(): Boolean {
        return try {
            val srv = ServerSocket()
            srv.reuseAddress = true
            srv.bind(InetSocketAddress("0.0.0.0", port))
            serverSocket = srv
            running = true
            statusRef.set("listening on :$port")
            Thread({ acceptLoop(srv) }, "copyclip-bridge-accept").apply {
                isDaemon = true
                start()
            }
            true
        } catch (t: Throwable) {
            statusRef.set("bind failed: ${t.message}")
            false
        }
    }

    fun stop() {
        running = false
        try {
            currentProc?.destroy()
        } catch (_: Throwable) {
        }
        try {
            serverSocket?.close()
        } catch (_: Throwable) {
        }
        statusRef.set("stopped")
    }

    private fun acceptLoop(srv: ServerSocket) {
        while (running) {
            val conn = try {
                srv.accept()
            } catch (t: Throwable) {
                if (running) statusRef.set("accept failed: ${t.message}")
                break
            }
            statusRef.set("client ${conn.inetAddress?.hostAddress ?: "?"}")
            try {
                serveClient(conn)
            } catch (t: Throwable) {
                statusRef.set("client error: ${t.message}")
            }
            if (running) statusRef.set("listening on :$port")
        }
    }

    private fun serveClient(conn: Socket) {
        conn.tcpNoDelay = true
        val input = conn.getInputStream().bufferedReader(Charsets.UTF_8)
        val output = conn.getOutputStream()

        fun writeLine(s: String) {
            output.write((s + "\n").toByteArray(Charsets.UTF_8))
            output.flush()
        }

        // Prove we hold the key before starting anything privileged.
        val nonce = Framing.newNonce()
        writeLine("HELLO $nonce")
        val authLine = input.readLine() ?: return
        val expected = "AUTH ${Framing.challengeResponse(hmacKey, nonce)}"
        if (authLine != expected) {
            writeLine("AUTH-FAIL")
            return
        }
        writeLine("AUTH-OK")

        // Then prove the client holds it too, so it cannot be tricked into talking to an
        // impostor server.
        val challengeLine = input.readLine() ?: return
        if (!challengeLine.startsWith("CHALLENGE ")) return
        val theirNonce = challengeLine.removePrefix("CHALLENGE ")
        writeLine("RESPONSE ${Framing.challengeResponse(hmacKey, theirNonce)}")

        // "me.mohit.copyclip.ClipBridge", not this app's own com.mohitarora.copyclip package,
        // on purpose: this names a class already compiled into clipbridge.dex (from
        // android/ClipBridge.java, which was never recompiled or renamed), not anything in
        // this app's own source. The two packages are independent by design -- app_process
        // just needs the exact class name the dex actually contains.
        val proc = ProcessBuilder("app_process", "/", "me.mohit.copyclip.ClipBridge", "serve")
            .apply { environment()["CLASSPATH"] = dexPath }
            .redirectErrorStream(true)
            .start()
        currentProc = proc

        var txSeq = 0L
        val pump = Thread({
            try {
                proc.inputStream.bufferedReader(Charsets.UTF_8).forEachLine { payload ->
                    if (payload.isNotEmpty()) {
                        txSeq++
                        writeLine(Framing.seal(hmacKey, txSeq, payload))
                    }
                }
            } catch (_: Throwable) {
            }
        }, "copyclip-bridge-pump")
        pump.isDaemon = true
        pump.start()

        var rxSeq = 0L
        try {
            val procIn = proc.outputStream
            while (true) {
                val line = input.readLine() ?: break
                if (line.isEmpty()) continue
                val (seq, payload) = try {
                    Framing.openFrame(hmacKey, line, rxSeq)
                } catch (e: Framing.AuthError) {
                    break
                }
                rxSeq = seq
                procIn.write((payload + "\n").toByteArray(Charsets.UTF_8))
                procIn.flush()
            }
        } finally {
            try {
                proc.destroy()
            } catch (_: Throwable) {
            }
            try {
                conn.close()
            } catch (_: Throwable) {
            }
            currentProc = null
        }
    }
}
