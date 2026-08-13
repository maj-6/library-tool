import { Button, ButtonGroup, Card, Icon, Tag, TextArea } from '@blueprintjs/core'
import type { DesignDirection } from '../types'

interface Props {
  design: DesignDirection
  feedback: string
  shortlisted: boolean
  onChange: (value: string) => void
  onShortlist: () => void
}

export function FeedbackPanel({ design, feedback, shortlisted, onChange, onShortlist }: Props) {
  return (
    <Card className="feedback-panel">
      <div className="feedback-panel__summary">
        <span className="feedback-panel__marker">{design.marker}</span>
        <div><span className="section-kicker">Review</span><h2>{design.title}</h2><p><strong>Use:</strong> {design.bestFor}. <strong>Limit:</strong> {design.tradeoff}.</p></div>
      </div>
      <div className="feedback-panel__form">
        <div className="feedback-panel__label"><span>Notes</span><Tag minimal icon="saved">Saved locally</Tag></div>
        <TextArea fill rows={2} value={feedback} placeholder={`Notes on ${design.title}…`} onChange={(event) => onChange(event.target.value)} />
        <ButtonGroup>
          <Button icon={shortlisted ? 'star' : 'star-empty'} intent={shortlisted ? 'warning' : 'none'} onClick={onShortlist}>{shortlisted ? 'Selected' : 'Select'}</Button>
          <Button icon="duplicate">Combine</Button>
          <Button icon="comment">Question</Button>
        </ButtonGroup>
      </div>
      <div className="feedback-panel__next"><Icon icon="flow-review" /><span><strong>Next:</strong> compare and select. Refine in stage 2.</span></div>
    </Card>
  )
}
