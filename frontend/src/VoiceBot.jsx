import React, { useState, useRef, useEffect, useCallback } from 'react'

// ---- Helpers ----------------------------------------------------------------

function formatTime(seconds) {
  const m = Math.floor(seconds / 60).toString().padStart(2, '0')
  const s = (seconds % 60).toString().padStart(2, '0')
  return `${m}:${s}`
}

function nowISO() {
  return new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function base64ToArrayBuffer(b64) {
  const binary = atob(b64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return bytes.buffer
}

/**
 * Downsample a Float32Array from sourceSampleRate → 16000 and convert to Int16.
 * Uses simple nearest-neighbour decimation (fine for speech capture).
 */
function float32ToInt16At16k(float32Array, sourceSampleRate) {
  const ratio = sourceSampleRate / 16000
  const outLen = Math.floor(float32Array.length / ratio)
  const out = new Int16Array(outLen)
  for (let i = 0; i < outLen; i++) {
    const src = float32Array[Math.floor(i * ratio)]
    const clamped = Math.max(-1, Math.min(1, src))
    out[i] = clamped < 0 ? clamped * 32768 : clamped * 32767
  }
  return out
}

// ---- Status dot -------------------------------------------------------------

function StatusDot({ status }) {
  const config = {
    idle:       { color: 'bg-gray-400',   label: 'Idle',       pulse: false },
    active:     { color: 'bg-green-400',  label: 'Active',     pulse: false },
    recording:  { color: 'bg-red-500',    label: 'Recording',  pulse: true  },
    processing: { color: 'bg-yellow-400', label: 'Processing', pulse: false },
    speaking:   { color: 'bg-blue-400',   label: 'Bot Speaking', pulse: true },
  }
  const { color, label, pulse } = config[status] || config.idle
  return (
    <div className="flex items-center gap-2">
      <span className={`inline-block w-3 h-3 rounded-full ${color} ${pulse ? 'animate-pulse' : ''}`} />
      <span className="text-gray-300 text-sm font-medium">{label}</span>
    </div>
  )
}

// ---- Audio queue for progressive playback -----------------------------------

class AudioQueue {
  constructor() {
    this.ctx = null
    this.nextPlayTime = 0
    this.playing = false
  }

  async init() {
    if (!this.ctx) {
      this.ctx = new (window.AudioContext || window.webkitAudioContext)()
    }
    if (this.ctx.state === 'suspended') {
      await this.ctx.resume()
    }
    return this.ctx
  }

  async enqueue(arrayBuffer) {
    const ctx = await this.init()
    try {
      const decoded = await ctx.decodeAudioData(arrayBuffer)
      const source = ctx.createBufferSource()
      source.buffer = decoded
      source.connect(ctx.destination)

      const now = ctx.currentTime
      const startAt = Math.max(now, this.nextPlayTime)
      source.start(startAt)
      this.nextPlayTime = startAt + decoded.duration
      this.playing = true
      source.onended = () => {
        if (ctx.currentTime >= this.nextPlayTime - 0.05) {
          this.playing = false
          this.nextPlayTime = 0
        }
      }
    } catch (e) {
      console.warn('Audio decode error:', e)
    }
  }

  stop() {
    if (this.ctx) {
      this.ctx.close().catch(() => {})
      this.ctx = null
    }
    this.nextPlayTime = 0
    this.playing = false
  }
}

// ---- Main component ---------------------------------------------------------

export default function VoiceBot() {
  // State
  const [callId, setCallId]               = useState(null)
  const [isCallActive, setIsCallActive]   = useState(false)
  const [botStatus, setBotStatus]         = useState('idle') // idle/active/recording/processing/speaking
  const [transcription, setTranscription] = useState('')
  const [botResponse, setBotResponse]     = useState('')
  const [conversation, setConversation]   = useState([])
  const [escalated, setEscalated]         = useState(false)
  const [ticketMessage, setTicketMessage] = useState('')
  const [callDuration, setCallDuration]   = useState(0)
  const [error, setError]                 = useState('')
  const [statusMsg, setStatusMsg]         = useState('Click "Start Call" to begin')
  const [customerPhone, setCustomerPhone] = useState('9876543210')
  const [customerName, setCustomerName]   = useState('')
  const [customerFound, setCustomerFound] = useState(false)
  const [lookedUpPhone, setLookedUpPhone] = useState('')

  // Refs
  const botResponseRef    = useRef('')
  const callIdRef         = useRef(null)
  const callTimerRef      = useRef(null)
  const errorTimerRef     = useRef(null)
  const conversationEndRef = useRef(null)
  // WebSocket + PCM capture refs
  const wsRef             = useRef(null)
  const audioCtxRef       = useRef(null)      // AudioContext for PCM capture
  const processorNodeRef  = useRef(null)       // ScriptProcessorNode
  const micStreamRef      = useRef(null)       // MediaStream
  const audioQueueRef     = useRef(new AudioQueue())
  const isBotSpeakingRef  = useRef(false)
  const pendingTurnRef    = useRef(null)       // { transcript, timestamp }

  // Sync callId state → ref
  useEffect(() => { callIdRef.current = callId }, [callId])

  // Auto-scroll conversation
  useEffect(() => {
    conversationEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [conversation])

  // Auto-dismiss errors
  useEffect(() => {
    if (error) {
      clearTimeout(errorTimerRef.current)
      errorTimerRef.current = setTimeout(() => setError(''), 6000)
    }
  }, [error])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      _teardown()
    }
  }, [])

  // ---- Low-level teardown --------------------------------------------------

  function _teardown() {
    clearInterval(callTimerRef.current)
    clearTimeout(errorTimerRef.current)

    // Stop ScriptProcessorNode
    if (processorNodeRef.current) {
      try { processorNodeRef.current.disconnect() } catch (_) {}
      processorNodeRef.current = null
    }
    // Stop AudioContext (capture)
    if (audioCtxRef.current) {
      audioCtxRef.current.close().catch(() => {})
      audioCtxRef.current = null
    }
    // Stop mic
    if (micStreamRef.current) {
      micStreamRef.current.getTracks().forEach(t => t.stop())
      micStreamRef.current = null
    }
    // Stop playback queue
    audioQueueRef.current.stop()
    audioQueueRef.current = new AudioQueue()
    // Close WebSocket
    if (wsRef.current) {
      wsRef.current.onclose = null
      wsRef.current.close()
      wsRef.current = null
    }
  }

  // ---- WebSocket message handler ------------------------------------------

  const handleWsMessage = useCallback(async (event) => {
    if (typeof event.data !== 'string') return
    let msg
    try { msg = JSON.parse(event.data) } catch { return }

    if (msg.type === 'transcript') {
      setTranscription(msg.text)
      setBotStatus('processing')
      setStatusMsg('Bot is thinking...')
      pendingTurnRef.current = { transcript: msg.text, timestamp: nowISO() }
    }

    else if (msg.type === 'response') {
      setBotResponse(msg.text)
      botResponseRef.current = msg.text
      if (msg.escalate) {
        setEscalated(true)
        const routing = msg.routing ? msg.routing.replace(/_/g, ' ') : 'support team'
        const ticket = msg.ticket_id || ''
        setTicketMessage(
          `Escalated to ${routing}${ticket ? ` | Ticket: ${ticket}` : ''} | Agent will call within 2 hours`
        )
      }
    }

    else if (msg.type === 'audio_chunk') {
      if (!isBotSpeakingRef.current) {
        isBotSpeakingRef.current = true
        setBotStatus('speaking')
        setStatusMsg('Bot is speaking...')

        // Commit pending turn to conversation
        const turn = pendingTurnRef.current
        if (turn) {
          setConversation(prev => [
            ...prev,
            { role: 'user', text: turn.transcript, timestamp: turn.timestamp },
          ])
          pendingTurnRef.current = null
        }
      }

      // Enqueue audio chunk for progressive playback
      const arrayBuffer = base64ToArrayBuffer(msg.audio_base64)
      await audioQueueRef.current.enqueue(arrayBuffer)

      if (msg.is_last) {
        // Add bot response to conversation after last chunk starts queuing
        const ts = nowISO()
        const responseText = botResponseRef.current
        setConversation(prev => [
          ...prev,
          { role: 'bot', text: responseText, timestamp: ts },
        ])

        // Wait for audio queue to drain, then switch back to listening
        const checkDone = setInterval(() => {
          if (!audioQueueRef.current.playing) {
            clearInterval(checkDone)
            isBotSpeakingRef.current = false
            setBotStatus('active')
            setStatusMsg('Listening...')
          }
        }, 200)
      }
    }

    else if (msg.type === 'error') {
      setError(`Pipeline error: ${msg.message}`)
      setBotStatus('active')
      setStatusMsg('Listening...')
    }
  }, [])

  // ---- Start PCM capture via ScriptProcessorNode --------------------------

  const startPcmCapture = useCallback(async () => {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 }
    })
    micStreamRef.current = stream

    // Try 16 kHz; if the browser uses a different rate we downsample in onaudioprocess
    let ctx
    try {
      ctx = new AudioContext({ sampleRate: 16000 })
    } catch {
      ctx = new (window.AudioContext || window.webkitAudioContext)()
    }
    audioCtxRef.current = ctx
    if (ctx.state === 'suspended') await ctx.resume()

    const source = ctx.createMediaStreamSource(stream)

    // bufferSize 2048 = 128 ms at 16 kHz (good balance of latency vs overhead)
    const bufferSize = 2048
    const proc = ctx.createScriptProcessor(bufferSize, 1, 1)
    processorNodeRef.current = proc

    proc.onaudioprocess = (e) => {
      const ws = wsRef.current
      if (!ws || ws.readyState !== WebSocket.OPEN) return

      const inputData = e.inputBuffer.getChannelData(0)  // Float32 at ctx.sampleRate
      const pcm16 = float32ToInt16At16k(inputData, ctx.sampleRate)
      ws.send(pcm16.buffer)
    }

    source.connect(proc)
    // Must connect to destination or some browsers skip onaudioprocess
    proc.connect(ctx.destination)
  }, [])

  // ---- Start call ----------------------------------------------------------

  const startCall = useCallback(async () => {
    setError('')
    setEscalated(false)
    setTicketMessage('')
    setConversation([])
    setTranscription('')
    setBotResponse('')
    botResponseRef.current = ''
    setCallDuration(0)
    setCustomerName('')
    setCustomerFound(false)

    try {
      setStatusMsg('Connecting...')
      setBotStatus('idle')

      // 1. HTTP POST /voice/start — get call_id + greeting
      const res = await fetch('/voice/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          customer_phone: customerPhone,
          customer_name: '',
          language: 'hi',
        }),
      })
      if (!res.ok) throw new Error(`Server error ${res.status}`)
      const data = await res.json()

      setCallId(data.call_id)
      callIdRef.current = data.call_id
      setIsCallActive(true)
      setCustomerName(data.customer_name || '')
      setCustomerFound(!!data.customer_found)
      setLookedUpPhone(data.looked_up_phone || '')

      // Play greeting audio
      if (data.greeting_audio_base64) {
        const buf = base64ToArrayBuffer(data.greeting_audio_base64)
        isBotSpeakingRef.current = true
        setBotStatus('speaking')
        setStatusMsg('Bot is speaking...')
        await audioQueueRef.current.enqueue(buf)
        // Wait for greeting to finish
        await new Promise(resolve => {
          const check = setInterval(() => {
            if (!audioQueueRef.current.playing) {
              clearInterval(check)
              resolve()
            }
          }, 200)
        })
        isBotSpeakingRef.current = false
      }

      // 2. Open WebSocket to the real-time pipeline
      const wsProtocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const wsUrl = `${wsProtocol}://${window.location.host}/ws/voice/${data.call_id}`
      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onmessage = handleWsMessage
      ws.onerror = (e) => {
        setError('WebSocket error — check backend logs')
        console.error('WS error', e)
      }
      ws.onclose = () => {
        if (isCallActive) {
          setBotStatus('idle')
          setStatusMsg('Connection closed')
        }
      }

      await new Promise((resolve, reject) => {
        ws.onopen = resolve
        ws.onerror = reject
      })

      // 3. Start PCM capture
      await startPcmCapture()

      // 4. Start call timer
      clearInterval(callTimerRef.current)
      let elapsed = 0
      callTimerRef.current = setInterval(() => {
        elapsed += 1
        setCallDuration(elapsed)
      }, 1000)

      setBotStatus('active')
      setStatusMsg('Listening...')

    } catch (err) {
      setError(`Failed to start call: ${err.message}`)
      setStatusMsg('Click "Start Call" to begin')
      _teardown()
    }
  }, [customerPhone, handleWsMessage, startPcmCapture])

  // ---- End call ------------------------------------------------------------

  const endCall = useCallback(async () => {
    const cid = callIdRef.current
    if (!cid) return

    _teardown()

    try {
      await fetch('/voice/end', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ call_id: cid, duration_seconds: callDuration }),
      })
    } catch (e) {
      console.warn('End call error:', e)
    }

    // Reset state
    setCallId(null)
    callIdRef.current = null
    setIsCallActive(false)
    setBotStatus('idle')
    setTranscription('')
    setBotResponse('')
    botResponseRef.current = ''
    setConversation([])
    setEscalated(false)
    setTicketMessage('')
    setCallDuration(0)
    setCustomerName('')
    setCustomerFound(false)
    setLookedUpPhone('')
    setError('')
    setStatusMsg('Call ended — click "Start Call" to begin a new call')
  }, [callDuration])

  // ---- Render ---------------------------------------------------------------
  const isProcessing = botStatus === 'processing'
  const isSpeaking   = botStatus === 'speaking'

  return (
    <div className="bg-gray-800 rounded-2xl shadow-2xl overflow-hidden">

      {/* ── Header ─────────────────────────────────────────────────────── */}
      <div className="bg-gray-900 px-6 py-4 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold" style={{ color: '#E40000' }}>
            airtel
          </h1>
          <p className="text-gray-400 text-sm">Customer Support</p>
        </div>
        <p className="text-gray-400 text-sm">Hindi · English · मराठी</p>
      </div>

      {/* ── Error banner ────────────────────────────────────────────────── */}
      {error && (
        <div className="bg-red-900 border-l-4 border-red-500 px-4 py-3 flex items-start gap-2">
          <span className="text-red-400 text-lg mt-0.5">⚠</span>
          <p className="text-red-200 text-sm">{error}</p>
        </div>
      )}

      {/* ── Status bar ──────────────────────────────────────────────────── */}
      <div className="bg-gray-750 border-b border-gray-700 px-6 py-3 flex items-center justify-between">
        <StatusDot status={botStatus} />
        <div className="flex items-center gap-4">
          {isCallActive && (
            <span className="text-green-400 font-mono text-sm">
              {formatTime(callDuration)}
            </span>
          )}
          {isProcessing && (
            <span className="inline-block w-4 h-4 border-2 border-yellow-400 border-t-transparent rounded-full animate-spin-slow" />
          )}
        </div>
      </div>

      {/* ── Main control area ───────────────────────────────────────────── */}
      <div className="px-6 py-8 flex flex-col items-center gap-6">

        {/* Status icon */}
        <div className="w-20 h-20 rounded-full flex items-center justify-center bg-gray-700 shadow-lg">
          <span className="text-3xl">
            {isSpeaking ? '🔊' : isProcessing ? '⏳' : isCallActive ? '👂' : '🎤'}
          </span>
        </div>

        {/* Status text */}
        <p className="text-gray-400 text-sm text-center min-h-5">
          {statusMsg}
        </p>

        {/* Pre-call: phone input. In-call: customer badge */}
        {!isCallActive ? (
          <div className="w-full max-w-xs">
            <input
              type="tel"
              value={customerPhone}
              onChange={(e) => setCustomerPhone(e.target.value.replace(/\D/g, '').slice(0, 10))}
              placeholder="Airtel number (e.g. 9876543210)"
              className="w-full bg-gray-700 border border-gray-600 text-gray-100 placeholder-gray-500 rounded-xl px-4 py-2 text-sm focus:outline-none focus:border-green-500"
            />
            <p className="text-xs text-gray-500 mt-1.5 text-center">
              Demo accounts: 9876543210, 9123456789, 9988776655
            </p>
          </div>
        ) : (
          <div
            className={`text-sm font-semibold px-4 py-1.5 rounded-full border ${
              customerFound
                ? 'bg-green-900/40 border-green-600 text-green-300'
                : 'bg-amber-900/40 border-amber-600 text-amber-300'
            }`}
          >
            {customerFound
              ? `✓ Identified: ${customerName}${lookedUpPhone ? ` (${lookedUpPhone})` : ''}`
              : '⚠ Guest — account questions need a registered number'}
          </div>
        )}

        {/* Start / End call buttons */}
        <div className="flex gap-4">
          {!isCallActive ? (
            <button
              onClick={startCall}
              className="bg-green-600 hover:bg-green-700 text-white font-semibold px-8 py-3 rounded-full transition-colors shadow-md"
            >
              📞 Start Call
            </button>
          ) : (
            <button
              onClick={endCall}
              className="bg-red-700 hover:bg-red-800 text-white font-semibold px-8 py-3 rounded-full transition-colors shadow-md"
            >
              📵 End Call
            </button>
          )}
        </div>
      </div>

      {/* ── Escalation banner ───────────────────────────────────────────── */}
      {escalated && (
        <div className="mx-6 mb-4 bg-yellow-900 border border-yellow-600 rounded-xl px-4 py-3 flex items-start gap-3">
          <span className="text-yellow-400 text-xl">⚠️</span>
          <div>
            <p className="text-yellow-200 font-semibold text-sm">Issue Escalated</p>
            <p className="text-yellow-300 text-sm mt-0.5">{ticketMessage}</p>
          </div>
        </div>
      )}

      {/* ── Live transcription / response preview ───────────────────────── */}
      {(transcription || botResponse) && (
        <div className="mx-6 mb-4 grid grid-cols-2 gap-3 text-xs">
          {transcription && (
            <div className="bg-blue-900 bg-opacity-40 border border-blue-700 rounded-lg px-3 py-2">
              <p className="text-blue-400 font-semibold mb-1">You said</p>
              <p className="text-blue-100">{transcription}</p>
            </div>
          )}
          {botResponse && (
            <div className="bg-gray-700 border border-gray-600 rounded-lg px-3 py-2">
              <p className="text-green-400 font-semibold mb-1">Airtel Bot</p>
              <p className="text-gray-100">{botResponse}</p>
            </div>
          )}
        </div>
      )}

      {/* ── Conversation history ────────────────────────────────────────── */}
      {conversation.length > 0 && (
        <div className="px-6 pb-6">
          <p className="text-gray-500 text-xs font-semibold uppercase tracking-wider mb-3">
            Conversation History
          </p>
          <div className="max-h-64 overflow-y-auto space-y-2 pr-1 scrollbar-thin">
            {conversation.map((turn, idx) => (
              <div
                key={idx}
                className={`flex ${turn.role === 'user' ? 'justify-end' : 'justify-start'}`}
              >
                <div
                  className={[
                    'max-w-xs rounded-2xl px-4 py-2 text-sm shadow',
                    turn.role === 'user'
                      ? 'bg-blue-600 text-white rounded-br-sm'
                      : 'bg-gray-700 text-gray-100 rounded-bl-sm',
                  ].join(' ')}
                >
                  <p className={`text-xs font-semibold mb-1 ${turn.role === 'user' ? 'text-blue-200' : 'text-green-400'}`}>
                    {turn.role === 'user' ? 'You' : 'Airtel Bot'}
                  </p>
                  <p>{turn.text}</p>
                  <p className={`text-xs mt-1 ${turn.role === 'user' ? 'text-blue-200' : 'text-gray-500'}`}>
                    {turn.timestamp}
                  </p>
                </div>
              </div>
            ))}
            <div ref={conversationEndRef} />
          </div>
        </div>
      )}

      {/* ── How to use ──────────────────────────────────────────────────── */}
      {!isCallActive && conversation.length === 0 && (
        <div className="px-6 pb-6">
          <div className="bg-gray-700 bg-opacity-50 rounded-xl p-4 text-xs text-gray-400 space-y-1">
            <p className="font-semibold text-gray-300 mb-2">How to use:</p>
            <p>1. Enter your Airtel number</p>
            <p>2. Click <strong className="text-green-400">Start Call</strong></p>
            <p>3. Speak naturally in Hindi, English, or Marathi — bot listens automatically</p>
            <p>4. Pause briefly — bot responds in your language</p>
            <p>5. Click <strong className="text-red-400">End Call</strong> when done</p>
          </div>
        </div>
      )}
    </div>
  )
}
