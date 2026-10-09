import { useEffect } from 'react'
import Editor, { loader } from '@monaco-editor/react'
import * as monaco from 'monaco-editor'
import EditorWorker from 'monaco-editor/editor/editor.worker.js?worker'
import JsonWorker from 'monaco-editor/language/json/json.worker.js?worker'
import CssWorker from 'monaco-editor/language/css/css.worker.js?worker'
import HtmlWorker from 'monaco-editor/language/html/html.worker.js?worker'
import TypescriptWorker from 'monaco-editor/language/typescript/ts.worker.js?worker'

self.MonacoEnvironment = {
  getWorker(_moduleId, label) {
    if (label === 'json') return new JsonWorker()
    if (['css', 'scss', 'less'].includes(label)) return new CssWorker()
    if (['html', 'handlebars', 'razor'].includes(label)) return new HtmlWorker()
    if (['typescript', 'javascript'].includes(label)) return new TypescriptWorker()
    return new EditorWorker()
  },
}
loader.config({ monaco })

const fileLanguage = (path: string, content: string) => {
  const filename = path.split('/').at(-1)?.toLowerCase() || ''
  const languages = monaco.languages.getLanguages()
  const named = languages.find(language => language.filenames?.some(name => name.toLowerCase() === filename))
  if (named) return named.id
  const extensions = languages.flatMap(language => (language.extensions || []).map(extension => ({ extension, language: language.id })))
    .sort((left, right) => right.extension.length - left.extension.length)
  const matched = extensions.find(({ extension }) => filename.endsWith(extension.toLowerCase()))
  if (matched) return matched.language
  const firstLine = content.split('\n', 1)[0]
  if (firstLine.startsWith('#!')) {
    if (/\bpython[\d.]*\b/.test(firstLine)) return 'python'
    if (/\b(?:bash|sh|zsh)\b/.test(firstLine)) return 'shell'
    if (/\bnode\b/.test(firstLine)) return 'javascript'
    if (/\bruby\b/.test(firstLine)) return 'ruby'
  }
  return 'plaintext'
}

export default function ProjectCodeEditor({ projectId, file, value, readOnly, onChange }: {
  projectId: string; file: string; value: string | undefined; readOnly: boolean; onChange: (value: string) => void
}) {
  const prefix = `file:///projects/${encodeURIComponent(projectId)}/`
  useEffect(() => () => {
    for (const model of monaco.editor.getModels()) {
      if (model.uri.toString().startsWith(prefix)) model.dispose()
    }
  }, [prefix])
  return <Editor
    height="100%"
    path={`${prefix}${file.split('/').map(encodeURIComponent).join('/')}`}
    language={fileLanguage(file, value || '')}
    value={value}
    theme="vs"
    loading={<div className="build-code-loading">正在加载代码编辑器…</div>}
    onChange={content => onChange(content ?? '')}
    options={{
      ariaLabel: `${file} 文件内容`, readOnly, automaticLayout: true,
      fontSize: 13, lineHeight: 21, fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace',
      minimap: { enabled: false }, scrollBeyondLastLine: false,
      lineNumbers: 'on', folding: true, tabSize: 2, insertSpaces: true,
      padding: { top: 12, bottom: 12 }, renderLineHighlight: 'line',
    }}
  />
}
