import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import ChatWorkspace, { type ChatProject } from '../src/ChatWorkspace'
import '../src/style.css'

function Harness() {
  const [project, setProject] = useState<ChatProject>({ id: 'fixture', title: '工作区回归测试', status: 'running', preview_html: '', published: false, model: 'fixture', messages: [] })
  const [draft, setDraft] = useState('')
  return <><output data-testid="project-status">{project.status}</output><ChatWorkspace project={project} setProject={setProject} draft={draft} setDraft={setDraft}
    projectMenu={null} toolsControl={null} tierControl={null} expertControl={null} modelControl={null} experts={[]}
    busy={false} publishBusy={false} sendMessage={async () => true} onChooseExperts={() => {}} onHome={() => {}} onRename={() => {}}
    onDelete={() => {}} onPublish={() => {}} onUnpublish={() => {}} onShare={() => {}} /></>
}
createRoot(document.getElementById('root')!).render(<Harness />)
