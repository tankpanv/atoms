import { useEffect, useRef, useState } from 'react'
import { LoaderCircle, Mic, Square } from 'lucide-react'
import { authFetch } from './auth'

export default function VoiceInput({ projectId, draft, setDraft, onError, className = 'build-round-button' }: { projectId?: string; draft: string; setDraft: (text: string) => void; onError: (text: string) => void; className?: string }) {
  const [state, setState] = useState<'idle' | 'recording' | 'transcribing'>('idle')
  const recorder = useRef<MediaRecorder | null>(null)
  const stream = useRef<MediaStream | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const controller = useRef<AbortController | null>(null)
  const alive = useRef(true)
  const latestDraft = useRef(draft)
  latestDraft.current = draft
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      controller.current?.abort()
      clearTimeout(timer.current)
      if (recorder.current) { recorder.current.onstop = null; if (recorder.current.state !== 'inactive') recorder.current.stop() }
      stream.current?.getTracks().forEach(track => track.stop())
    }
  }, [projectId])
  const start = async () => {
    if (state === 'recording') { recorder.current?.stop(); return }
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { onError('麦克风需要 HTTPS 或 localhost，并使用支持录音的浏览器。'); return }
    setState('transcribing')
    try {
      const audioStream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (!alive.current) { audioStream.getTracks().forEach(track => track.stop()); return }
      stream.current = audioStream
      const mime = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'].find(type => MediaRecorder.isTypeSupported(type))
      if (!mime) throw new Error('当前浏览器不支持所需的录音格式')
      const recording = new MediaRecorder(audioStream, { mimeType: mime })
      recorder.current = recording
      const chunks: Blob[] = []
      recording.ondataavailable = event => { if (event.data.size) chunks.push(event.data) }
      recording.onstop = () => {
        clearTimeout(timer.current)
        audioStream.getTracks().forEach(track => track.stop())
        if (!alive.current) return
        setState('transcribing')
        void (async () => {
          try {
            const blob = new Blob(chunks, { type: recording.mimeType })
            if (!blob.size || blob.size > 10 * 1024 * 1024) throw new Error('录音为空或超过 10 MB，请重新录制。')
            const encoded = await new Promise<string>((resolve, reject) => {
              const reader = new FileReader()
              reader.onload = () => resolve(String(reader.result).split(',')[1])
              reader.onerror = () => reject(new Error('无法读取录音'))
              reader.readAsDataURL(blob)
            })
            if (!alive.current) return
            controller.current = new AbortController()
            const format = recording.mimeType.includes('ogg') ? 'ogg' : recording.mimeType.includes('mp4') ? 'm4a' : 'webm'
            const response = await authFetch(projectId ? `/projects/${projectId}/transcribe` : '/transcribe', { method: 'POST', signal: controller.current.signal, body: JSON.stringify({ data: encoded, format }) })
            const result = await response.json() as { text?: string; detail?: string }
            if (!response.ok) throw new Error(result.detail || '语音转换失败')
            if (alive.current && result.text?.trim()) setDraft(`${latestDraft.current}${latestDraft.current && !/\s$/.test(latestDraft.current) ? ' ' : ''}${result.text.trim()}`)
            else if (alive.current) onError('没有识别到语音，请重新录制。')
          } catch (error) { if (alive.current) onError((error as Error).message) }
          finally { if (alive.current) setState('idle') }
        })()
      }
      recording.onerror = () => { recording.onstop = null; audioStream.getTracks().forEach(track => track.stop()); clearTimeout(timer.current); if (alive.current) { setState('idle'); onError('录音失败，请重试。') } }
      recording.start()
      setState('recording')
      timer.current = setTimeout(() => { if (recording.state === 'recording') recording.stop() }, 120000)
    } catch (error) { stream.current?.getTracks().forEach(track => track.stop()); if (alive.current) { setState('idle'); onError(`无法使用麦克风：${(error as Error).message}`) } }
  }
  return <button type="button" className={`${className} ${state === 'recording' ? 'voice-recording' : ''}`} disabled={state === 'transcribing'} title={state === 'recording' ? '停止录音并转换文字' : state === 'transcribing' ? '正在转换语音…' : '语音输入'} aria-label={state === 'recording' ? '停止录音并转换文字' : state === 'transcribing' ? '正在转换语音…' : '语音输入'} onClick={() => void start()}>{state === 'recording' ? <Square size={15} /> : state === 'transcribing' ? <LoaderCircle size={16} /> : <Mic size={16} />}</button>
}
