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
        <div><span className="section-kicker">Design review</span><h2>{design.title}</h2><p><strong>Best for:</strong> {design.bestFor}. <strong>Trade-off:</strong> {design.tradeoff}.</p></div>
      </div>
      <div className="feedback-panel__form">
        <div className="feedback-panel__label"><span>What should we keep, change, or combine?</span><Tag minimal icon="saved">autosaved in this browser</Tag></div>
        <TextArea fill rows={2} value={feedback} placeholder={`Notes on ${design.title}…`} onChange={(event) => onChange(event.target.value)} />
        <ButtonGroup>
          <Button icon={shortlisted ? 'star' : 'star-empty'} intent={shortlisted ? 'warning' : 'none'} onClick={onShortlist}>{shortlisted ? 'Shortlisted' : 'Add to shortlist'}</Button>
          <Button icon="duplicate">Borrow a feature</Button>
          <Button icon="comment">Add question</Button>
        </ButtonGroup>
      </div>
      <div className="feedback-panel__next"><Icon icon="flow-review" /><span><strong>Next:</strong> compare, annotate, and shortlist. The chosen direction will be refined in Stage 2 before implementation.</span></div>
    </Card>
  )
}
