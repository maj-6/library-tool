import { useState } from 'react'
import { Button, Callout, Checkbox, HTMLSelect, Tag, TextArea } from '@blueprintjs/core'

export function ReprocessPanel() {
  const [queued, setQueued] = useState(false)
  const [instructions, setInstructions] = useState('Treat red rubric as a separate heading. Use Hand B from the March section onward; exclude faint right-margin marks from body text.')
  return (
    <section className="panel-section reprocess-panel">
      <div className="section-heading">
        <div><span className="section-kicker">Guided automation</span><h3>Reprocess selection</h3></div>
        <Tag minimal intent="warning">uses annotations</Tag>
      </div>
      {queued ? (
        <Callout intent="success" icon="tick-circle" title="Reprocessing job staged">
          This prototype records the region constraints and instructions; it does not call an OCR service.
          <Button small minimal onClick={() => setQueued(false)}>Edit request</Button>
        </Callout>
      ) : <>
        <label className="field-label">Engine</label>
        <HTMLSelect fill options={['Mistral OCR 4 · layout blocks', 'Local OCR · Tesseract 5.4', 'Custom paleography pass']} />
        <label className="field-label">Editor instructions</label>
        <TextArea fill rows={4} value={instructions} onChange={(event) => setInstructions(event.target.value)} />
        <Checkbox defaultChecked label="Respect custom region hierarchy" />
        <Checkbox defaultChecked label="Preserve existing approved text" />
        <Button fill intent="primary" icon="refresh" onClick={() => setQueued(true)}>Stage guided reprocessing</Button>
      </>}
    </section>
  )
}
