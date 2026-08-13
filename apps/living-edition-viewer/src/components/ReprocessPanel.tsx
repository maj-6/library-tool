import { useState } from 'react'
import { Button, Callout, Checkbox, HTMLSelect, Tag, TextArea } from '@blueprintjs/core'

export function ReprocessPanel() {
  const [queued, setQueued] = useState(false)
  const [instructions, setInstructions] = useState('Treat red rubric as a separate heading. Use Hand B from the March section onward; exclude faint right-margin marks from body text.')
  return (
    <section className="panel-section reprocess-panel">
      <div className="section-heading">
        <div><span className="section-kicker">Processing</span><h3>Reprocess</h3></div>
        <Tag minimal intent="warning">Region-aware</Tag>
      </div>
      {queued ? (
        <Callout intent="success" icon="tick-circle" title="Request staged">
          No OCR service called.
          <Button small minimal onClick={() => setQueued(false)}>Edit</Button>
        </Callout>
      ) : <>
        <label className="field-label">Engine</label>
        <HTMLSelect fill options={['Mistral OCR 4 · layout blocks', 'Local OCR · Tesseract 5.4', 'Custom paleography pass']} />
        <label className="field-label">Instructions</label>
        <TextArea fill rows={4} value={instructions} onChange={(event) => setInstructions(event.target.value)} />
        <Checkbox defaultChecked label="Use region hierarchy" />
        <Checkbox defaultChecked label="Keep approved text" />
        <Button fill intent="primary" icon="refresh" onClick={() => setQueued(true)}>Stage</Button>
      </>}
    </section>
  )
}
