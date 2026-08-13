import { useEffect, useMemo, useState } from 'react'
import type { ComponentType, ReactNode } from 'react'
import {
  Button,
  ButtonGroup,
  Card,
  Divider,
  HTMLSelect,
  Icon,
  Tag,
} from '@blueprintjs/core'
import {
  readerAudienceDefinitions,
  readerAccessPreferenceDefinitions,
  readerCompositionPresets,
  readerMaterialDefinitions,
  readerPresentationDefinitions,
  readerViewportDefinitions,
} from '../data/registries'
import {
  defaultReaderPublicationId,
  herbalReaderPublication,
  isManuscriptReaderPayload,
  isReferenceReaderPayload,
  readerPublications,
} from '../data/readerData'
import { resolveReaderComposition } from '../data/readerResolver'
import type {
  ManuscriptReaderPayload,
  ReaderAdapterInstance,
  ReaderAudienceDefinition,
  ReaderEntity,
  ReaderPresentationId,
  ReaderPublication,
  ReaderSection,
  ReferenceReaderPayload,
} from '../types'
import './ReaderPreview.css'

interface Props {
  shellId: string
}

interface SavedReaderState {
  publicationId?: string
  audienceId?: string
  accessPreferenceId?: string
  presentationId?: string
  viewportId?: string
  showNotes?: boolean
  largeText?: boolean
  highContrast?: boolean
}

function readSavedReaderState(): SavedReaderState {
  try {
    return JSON.parse(window.localStorage.getItem('whl-design.reader-state') ?? '{}') as SavedReaderState
  } catch {
    return {}
  }
}

export function ReaderPreview({ shellId }: Props) {
  const savedState = readSavedReaderState()
  const [publicationId, setPublicationId] = useState<string>(() => (
    readerPublications.some((item) => item.id === savedState.publicationId)
      ? savedState.publicationId!
      : defaultReaderPublicationId
  ))
  const [audienceId, setAudienceId] = useState<string>(() => (
    readerAudienceDefinitions.some((item) => item.id === savedState.audienceId)
      ? savedState.audienceId!
      : 'scholarly'
  ))
  const [accessPreferenceId, setAccessPreferenceId] = useState<string>(() => (
    readerAccessPreferenceDefinitions.some((item) => item.id === savedState.accessPreferenceId)
      ? savedState.accessPreferenceId!
      : 'standard'
  ))
  const [requestedPresentationId, setRequestedPresentationId] = useState<string>(() => (
    readerPresentationDefinitions.some((item) => item.id === savedState.presentationId)
      ? savedState.presentationId!
      : 'parallel'
  ))
  const [viewportId, setViewportId] = useState<string>(() => (
    readerViewportDefinitions.some((item) => item.id === savedState.viewportId)
      ? savedState.viewportId!
      : 'desktop'
  ))
  const [adapterId, setAdapterId] = useState<string>('herbal-manuscript')
  const [selectionId, setSelectionId] = useState<string>('')
  const [showNotes, setShowNotes] = useState(savedState.showNotes ?? false)
  const [largeText, setLargeText] = useState(savedState.largeText ?? false)
  const [highContrast, setHighContrast] = useState(savedState.highContrast ?? false)
  const [sitePreview, setSitePreview] = useState(false)

  const publication = readerPublications.find((item) => item.id === publicationId)
    ?? herbalReaderPublication
  const resolution = useMemo(
    () => resolveReaderComposition(
      audienceId,
      requestedPresentationId,
      publication,
    ),
    [audienceId, publication, requestedPresentationId],
  )
  const adapterRuntimes = useMemo(
    () => createAdapterRuntimes(publication),
    [publication],
  )
  const runtime = useMemo(() => {
    const effectiveId = resolution.effective?.id
    const selected = adapterRuntimes.find((item) => item.adapterId === adapterId)
    if (selected && (!effectiveId || selected.presentationIds.includes(effectiveId))) {
      return selected
    }
    return adapterRuntimes.find((item) => (
      effectiveId ? item.presentationIds.includes(effectiveId) : true
    )) ?? selected ?? adapterRuntimes[0] ?? null
  }, [adapterId, adapterRuntimes, resolution.effective?.id])
  const viewport = readerViewportDefinitions.find((item) => item.id === viewportId)
    ?? readerViewportDefinitions[0]
  const accessPreference = readerAccessPreferenceDefinitions.find(
    (item) => item.id === accessPreferenceId,
  ) ?? readerAccessPreferenceDefinitions[0]

  useEffect(() => {
    window.localStorage.setItem('whl-design.reader-state', JSON.stringify({
      publicationId,
      audienceId,
      accessPreferenceId,
      presentationId: requestedPresentationId,
      viewportId,
      showNotes,
      largeText,
      highContrast,
    } satisfies SavedReaderState))
  }, [
    accessPreferenceId,
    audienceId,
    highContrast,
    largeText,
    publicationId,
    requestedPresentationId,
    showNotes,
    viewportId,
  ])

  useEffect(() => {
    setAdapterId(publication.adapters[0]?.id ?? '')
  }, [publication])

  useEffect(() => {
    setSelectionId(runtime?.initialSelectionId ?? '')
  }, [publication.id, runtime?.adapterId, runtime?.initialSelectionId])

  const choosePreset = (presetId: string) => {
    const preset = readerCompositionPresets.find((item) => item.id === presetId)
    if (!preset) return
    setPublicationId(preset.publicationId)
    setAudienceId(preset.audienceId)
    setRequestedPresentationId(preset.presentationId)
    applyAccessPreference(preset.accessPreferenceId)
  }

  function applyAccessPreference(id: string) {
    const preference = readerAccessPreferenceDefinitions.find((item) => item.id === id)
    if (!preference) return
    setAccessPreferenceId(id)
    const features: readonly string[] = preference.features
    setLargeText(features.includes('large-text'))
    setHighContrast(features.includes('high-contrast'))
    setShowNotes(features.includes('notes-expanded'))
  }

  const frame = runtime
    && resolution.audience
    && resolution.material
    && resolution.effective
    ? (
        <ReaderFrame
          publication={publication}
          runtime={runtime}
          audience={resolution.audience}
          materialLabel={resolution.material.label}
          presentationId={resolution.effective.id}
          viewportId={viewport.id}
          frameWidth={viewport.frameWidth}
          showNotes={showNotes}
          largeText={largeText}
          highContrast={highContrast}
          selectionId={selectionId}
          onSelect={setSelectionId}
        />
      )
    : (
        <BlockedPresentation
          message={
            adapterRuntimes.length > 0
              ? resolution.explanation
              : 'No runtime adapter contribution can interpret this projection payload.'
          }
        />
      )

  if (sitePreview) {
    return (
      <section className="reader-site-preview" aria-label="Visual public-site fixture">
        <Tag className="reader-site-preview__notice" intent="warning" icon="lab-test">
          Visual site fixture · same DOM, not an isolation or security boundary
        </Tag>
        <Button
          className="reader-site-preview__return"
          icon="arrow-left"
          text="Return to workbench"
          onClick={() => setSitePreview(false)}
        />
        {frame}
      </section>
    )
  }

  return (
    <section className="reader-preview" aria-label="Reader publication preview">
      <header className="reader-editorbar">
        <div className="reader-editorbar__title">
          <span className="reader-editorbar__mark"><Icon icon="eye-open" /></span>
          <span>
            <strong>Reader Preview</strong>
            <small>{shellId} shell · read-only composition</small>
          </span>
        </div>
        <div className="reader-release">
          <Tag intent="warning" icon="lab-test">Projection fixture</Tag>
          <span>
            <strong>Citation preview · not published</strong>
            <small>{publication.projectionId} · {publication.projection.build.state}</small>
          </span>
        </div>
        <div className="reader-editorbar__actions">
          <Button
            small
            icon="globe-network"
            text="Site preview"
            onClick={() => setSitePreview(true)}
          />
          <Button small icon="print" text="Print" onClick={() => window.print()} />
        </div>
      </header>

      <div className="reader-presets" aria-label="Representative reader compositions">
        <span>Profiles</span>
        {readerCompositionPresets.map((preset) => (
          <Button
            key={preset.id}
            small
            minimal
            active={
              preset.publicationId === publication.id
              && preset.audienceId === audienceId
              && preset.presentationId === requestedPresentationId
              && preset.accessPreferenceId === accessPreferenceId
            }
            text={preset.label}
            title={preset.detail}
            onClick={() => choosePreset(preset.id)}
          />
        ))}
      </div>

      <div className="reader-toolbar">
        <label>
          <span>Projection</span>
          <HTMLSelect
            minimal
            value={publication.id}
            onChange={(event) => setPublicationId(event.currentTarget.value)}
            options={readerPublications.map((item) => ({
              value: item.id,
              label: item.title,
            }))}
          />
        </label>
        <label>
          <span>Audience</span>
          <HTMLSelect
            minimal
            value={audienceId}
            onChange={(event) => setAudienceId(event.currentTarget.value)}
            options={readerAudienceDefinitions.map((item) => ({
              value: item.id,
              label: item.label,
            }))}
          />
        </label>
        <label>
          <span>Access</span>
          <HTMLSelect
            minimal
            value={accessPreferenceId}
            onChange={(event) => applyAccessPreference(event.currentTarget.value)}
            options={readerAccessPreferenceDefinitions.map((item) => ({
              value: item.id,
              label: item.label,
            }))}
          />
        </label>
        {publication.adapters.length > 1 && (
          <label>
            <span>Adapter</span>
            <HTMLSelect
              minimal
              value={runtime?.adapterId ?? adapterId}
              onChange={(event) => setAdapterId(event.currentTarget.value)}
              options={adapterRuntimes.map((item) => ({ value: item.adapterId, label: item.label }))}
            />
          </label>
        )}
        <Tag minimal icon={resolution.material?.icon}>{resolution.material?.label ?? 'Unknown material'}</Tag>
        <Divider />
        <div className="reader-toolbar__modes" aria-label="Presentation mode">
          <span>Presentation</span>
          <ButtonGroup minimal>
            {readerPresentationDefinitions.map((item) => {
              const compatible = resolution.compatible.some(
                (candidate) => candidate.id === item.id,
              )
              return (
                <Button
                  key={item.id}
                  small
                  icon={item.icon}
                  text={item.label}
                  active={resolution.effective?.id === item.id}
                  intent={!compatible && requestedPresentationId === item.id ? 'warning' : 'none'}
                  className={compatible ? '' : 'reader-mode--incompatible'}
                  title={compatible ? item.detail : item.label + ' is not supported by this projection'}
                  onClick={() => setRequestedPresentationId(item.id)}
                />
              )
            })}
          </ButtonGroup>
        </div>
        <span className="reader-toolbar__spacer" />
        <ButtonGroup minimal aria-label="Preview size">
          {readerViewportDefinitions.map((item) => (
            <Button
              key={item.id}
              small
              icon={item.icon}
              active={viewportId === item.id}
              aria-label={item.label + ' preview'}
              title={item.label + ' preview'}
              onClick={() => setViewportId(item.id)}
            />
          ))}
        </ButtonGroup>
        <Divider />
        <Button
          small
          minimal
          icon="font"
          active={largeText}
          text="Larger text"
          onClick={() => {
            setAccessPreferenceId('custom')
            setLargeText((current) => !current)
          }}
        />
        <Button
          small
          minimal
          icon="contrast"
          active={highContrast}
          aria-label="High contrast"
          title="High contrast"
          onClick={() => {
            setAccessPreferenceId('custom')
            setHighContrast((current) => !current)
          }}
        />
      </div>

      <div
        className={resolution.state === 'compatible' ? 'reader-compatibility' : 'reader-compatibility is-fallback'}
        aria-live="polite"
      >
        <Icon icon={resolution.state === 'compatible' ? 'tick-circle' : 'warning-sign'} />
        <span>
          <strong>{resolution.explanation}</strong>
          <small>
            Compatible: {resolution.compatible.map((item) => item.label).join(', ') || 'none'}.
            {' '}Declared material, projection capabilities, rights, and policy applied.
          </small>
        </span>
        <Tag minimal>{resolution.audience?.apparatus ?? 'blocked'} apparatus</Tag>
        <Tag minimal icon={accessPreference.icon}>{accessPreference.label}</Tag>
      </div>

      <ProjectionDetails publication={publication} />

      <div className="reader-preview-stage">{frame}</div>

      <footer className="reader-projectionbar">
        <span><Icon icon="lock" /> {publication.projectionNotice}</span>
        <span>{publication.projectionId}</span>
        <Button
          minimal
          small
          icon={showNotes ? 'eye-off' : 'annotation'}
          text={showNotes ? 'Hide notes' : 'Notes on demand'}
          onClick={() => {
            setAccessPreferenceId('custom')
            setShowNotes((current) => !current)
          }}
        />
      </footer>
    </section>
  )
}

function ProjectionDetails({ publication }: { publication: ReaderPublication }) {
  const projection = publication.projection
  return (
    <div className="reader-diagnostics" aria-label="Projection details and problems">
      <details>
        <summary>
          <span><Icon icon="layers" /> Projection</span>
          <Tag minimal>{projection.layerPins.length} pins</Tag>
        </summary>
        <div className="reader-diagnostics__body">
          <div className="reader-diagnostics__facts">
            <span>
              <strong>Rights</strong>
              {projection.rights.access} · {projection.rights.reproduction}
              {' '}Preview modes: {projection.rights.allowedPresentationIds.join(', ')}
            </span>
            <span><strong>Entities</strong>{projection.entityReleaseId ?? 'No entity release'}</span>
            <span><strong>Build</strong>{projection.build.state} · {projection.build.generatedAt}</span>
            <span><strong>Fidelity</strong>{projection.build.fidelity}</span>
          </div>
          <div className="reader-diagnostics__pins">
            {projection.layerPins.map((pin) => (
              <span key={pin.kind + ':' + pin.layerId}>
                <strong>{pin.label}</strong> {pin.layerId}@{pin.revision}
              </span>
            ))}
          </div>
        </div>
      </details>
      <details>
        <summary>
          <span><Icon icon="issue" /> Problems</span>
          <Tag intent="danger" minimal>{projection.problems.length}</Tag>
        </summary>
        <div className="reader-diagnostics__body reader-diagnostics__problems">
          <p><Tag intent="danger" minimal>No public release</Tag> {projection.publicRelease.state} · no canonical public URL</p>
          {projection.problems.map((problem) => (
            <p key={problem.id}><strong>{problem.id} · {problem.severity}</strong>{problem.message}</p>
          ))}
          <strong>Exclusions</strong>
          <ul>{projection.exclusions.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>
      </details>
    </div>
  )
}

interface ReaderKernelPresentationProps {
  publication: ReaderPublication
  audience: ReaderAudienceDefinition
  showNotes: boolean
  selectionId: string
  onSelect: (id: string) => void
}

interface ReaderAdapterRuntime {
  adapterId: string
  kind: string
  label: string
  initialSelectionId: string
  presentationIds: readonly ReaderPresentationId[]
  render: (presentationId: ReaderPresentationId, props: ReaderKernelPresentationProps) => ReactNode
}

interface ReaderAdapterFactory {
  id: string
  create: (
    publication: ReaderPublication,
    adapter: ReaderAdapterInstance,
  ) => ReaderAdapterRuntime | null
}

const readerAdapterFactoryRegistry: Readonly<Record<string, ReaderAdapterFactory>> = {
  manuscript: {
    id: 'manuscript-reader-runtime',
    create: createManuscriptRuntime,
  },
  reference: {
    id: 'reference-reader-runtime',
    create: createReferenceRuntime,
  },
}

function createAdapterRuntimes(publication: ReaderPublication): ReaderAdapterRuntime[] {
  return publication.adapters.flatMap((adapter) => {
    const factory = readerAdapterFactoryRegistry[adapter.kind]
    const runtime = factory?.create(publication, adapter) ?? null
    return runtime ? [runtime] : []
  })
}

function createManuscriptRuntime(
  _publication: ReaderPublication,
  adapter: ReaderAdapterInstance,
): ReaderAdapterRuntime | null {
  if (adapter.kind !== 'manuscript' || !isManuscriptReaderPayload(adapter.payload)) return null
  const payload = adapter.payload
  return {
    adapterId: adapter.id,
    kind: adapter.kind,
    label: payload.folio,
    initialSelectionId: payload.sourceRegions[0]?.id ?? '',
    presentationIds: Object.keys(manuscriptPresentationRegistry),
    render: (presentationId, props) => {
      const Renderer = manuscriptPresentationRegistry[presentationId]
      return Renderer
        ? <Renderer {...props} payload={payload} />
        : <UnsupportedAdapterPresentation adapterKind={adapter.kind} presentationId={presentationId} />
    },
  }
}

function createReferenceRuntime(
  _publication: ReaderPublication,
  adapter: ReaderAdapterInstance,
): ReaderAdapterRuntime | null {
  if (adapter.kind !== 'reference' || !isReferenceReaderPayload(adapter.payload)) return null
  const payload = adapter.payload
  return {
    adapterId: adapter.id,
    kind: adapter.kind,
    label: payload.volumeLabel,
    initialSelectionId: payload.entries[0]?.id ?? '',
    presentationIds: Object.keys(referencePresentationRegistry),
    render: (presentationId, props) => {
      const Renderer = referencePresentationRegistry[presentationId]
      return Renderer
        ? <Renderer {...props} payload={payload} />
        : <UnsupportedAdapterPresentation adapterKind={adapter.kind} presentationId={presentationId} />
    },
  }
}

interface ReaderFrameProps extends ReaderKernelPresentationProps {
  runtime: ReaderAdapterRuntime
  materialLabel: string
  presentationId: ReaderPresentationId
  viewportId: string
  frameWidth: string
  largeText: boolean
  highContrast: boolean
}

function ReaderFrame(props: ReaderFrameProps) {
  const classes = [
    'public-reader',
    'public-reader--' + props.viewportId,
    props.largeText ? 'is-large-text' : '',
    props.highContrast ? 'is-high-contrast' : '',
  ].filter(Boolean).join(' ')

  return (
    <article className={classes} style={{ maxWidth: props.frameWidth }}>
      <PublicReaderHeader
        publication={props.publication}
        adapterId={props.runtime.adapterId}
        materialLabel={props.materialLabel}
        presentationId={props.presentationId}
      />
      <main className="public-reader__main">
        {props.runtime.render(props.presentationId, props)}
        <div className="reader-global-apparatus">
          <EntityApparatus publication={props.publication} />
          <CitationPanel publication={props.publication} />
        </div>
      </main>
      <footer className="public-reader__footer" id="reader-about">
        <span>World Herb Library · projection fixture</span>
        <span>{props.publication.releaseLabel} · {props.publication.releaseId}</span>
      </footer>
    </article>
  )
}

function PublicReaderHeader({
  publication,
  adapterId,
  materialLabel,
  presentationId,
}: {
  publication: ReaderPublication
  adapterId: string
  materialLabel: string
  presentationId: string
}) {
  const mode = readerPresentationDefinitions.find((item) => item.id === presentationId)
  const scopedStructures = publication.structures
    .filter((item) => item.adapterId === adapterId)
    .slice()
    .sort((left, right) => left.order - right.order)
  const structure = scopedStructures.find((item) => item.parentId !== null)
    ?? scopedStructures[0]
  const target = publication.targets.find((item) => item.adapterId === adapterId)
  return (
    <header className="public-reader__header">
      <div className="public-reader__brand"><i>WHL</i><span>World Herb Library</span></div>
      <nav aria-label="Reader sections">
        <a href="#reader-text">Text</a>
        <a href="#reader-entities">Entities</a>
        <a href="#reader-citation">Citation</a>
        <a href="#reader-about">About</a>
      </nav>
      <div className="public-reader__title">
        <span>{publication.subtitle}</span>
        <h1>{publication.title}</h1>
        <p>
          {publication.repository} · {publication.shelfmark}
          {structure ? ' · ' + structure.label : ''}
          {target ? ' · ' + target.label : ''}
        </p>
      </div>
      <div className="public-reader__meta">
        <Tag minimal intent="warning" icon="lab-test">Projection fixture</Tag>
        <Tag minimal>{materialLabel}</Tag>
        <Tag minimal icon={mode?.icon}>{mode?.label}</Tag>
      </div>
    </header>
  )
}

function UnsupportedAdapterPresentation({
  adapterKind,
  presentationId,
}: {
  adapterKind: string
  presentationId: string
}) {
  return (
    <BlockedPresentation
      message={'The ' + adapterKind + ' adapter contributes no “' + presentationId + '” renderer.'}
    />
  )
}

function BlockedPresentation({ message }: { message: string }) {
  return (
    <div className="reader-blocked" role="status">
      <Icon icon="disable" size={28} />
      <h2>Reader composition unavailable</h2>
      <p>{message}</p>
      <small>No incompatible or unknown renderer was selected.</small>
    </div>
  )
}

interface ManuscriptPresentationProps extends ReaderKernelPresentationProps {
  payload: ManuscriptReaderPayload
}

const manuscriptPresentationRegistry: Readonly<Record<
  string,
  ComponentType<ManuscriptPresentationProps>
>> = {
  reading: ManuscriptReadingPresentation,
  facsimile: ManuscriptFacsimilePresentation,
  parallel: ManuscriptParallelPresentation,
  compare: ManuscriptComparePresentation,
}

function ManuscriptReadingPresentation(props: ManuscriptPresentationProps) {
  return (
    <ReadingLayout
      publication={props.publication}
      sections={props.payload.sections}
      showNotes={props.showNotes}
      deck="A continuous modern-English path through the selected passage, with evidence and notes available without interrupting the reading."
    />
  )
}

function ManuscriptFacsimilePresentation(props: ManuscriptPresentationProps) {
  const selected = props.payload.sourceRegions.find(
    (region) => region.id === props.selectionId,
  ) ?? props.payload.sourceRegions[0]
  if (!selected) return <BlockedPresentation message="This manuscript adapter has no readable regions." />
  return (
    <div className="reader-facsimile" id="reader-text">
      <div className="reader-facsimile__stage">
        <SourceLeaf
          folio={props.payload.folio}
          regions={props.payload.sourceRegions}
          selectedRegionId={selected.id}
          onSelectRegion={props.onSelect}
        />
      </div>
      <aside className="reader-text-drawer">
        <div className="reader-text-drawer__head">
          <span><Icon icon="link" /> Synchronized text</span>
          <Tag minimal>{selected.id}</Tag>
        </div>
        <small>{selected.label}</small>
        <h2>Source</h2>
        <p className="reader-source-text">
          <EntityLinkedText publication={props.publication} entityIds={selected.entityIds} text={selected.sourceText} />
        </p>
        <h2>Edited reading</h2>
        <p>
          <EntityLinkedText publication={props.publication} entityIds={selected.entityIds} text={selected.transcription} />
        </p>
        <h2>Modern English</h2>
        <p>
          <EntityLinkedText publication={props.publication} entityIds={selected.entityIds} text={selected.translation} />
        </p>
        <div className="reader-region-nav">
          {props.payload.sourceRegions.map((region) => (
            <button
              key={region.id}
              className={region.id === selected.id ? 'is-active' : ''}
              onClick={() => props.onSelect(region.id)}
            >
              {region.id}
            </button>
          ))}
        </div>
      </aside>
    </div>
  )
}

function ManuscriptParallelPresentation(props: ManuscriptPresentationProps) {
  return (
    <div className="reader-parallel" id="reader-text">
      <div className="reader-parallel__source">
        <SourceLeaf
          folio={props.payload.folio}
          regions={props.payload.sourceRegions}
          selectedRegionId={props.selectionId}
          onSelectRegion={props.onSelect}
        />
      </div>
      <div className="reader-parallel__columns">
        <header><span>Region</span><span>Transcription</span><span>Modern English</span></header>
        {props.payload.sourceRegions.map((region) => (
          <div
            key={region.id}
            className={region.id === props.selectionId ? 'reader-parallel__row is-active' : 'reader-parallel__row'}
          >
            <button
              className="reader-parallel__select"
              onClick={() => props.onSelect(region.id)}
              aria-pressed={region.id === props.selectionId}
            >
              <strong>{region.id}</strong><small>{region.label}</small>
            </button>
            <p>
              <EntityLinkedText publication={props.publication} entityIds={region.entityIds} text={region.transcription} />
            </p>
            <span>
              <p>
                <EntityLinkedText publication={props.publication} entityIds={region.entityIds} text={region.translation} />
              </p>
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

function ManuscriptComparePresentation(props: ManuscriptPresentationProps) {
  const transcriptionPins = props.publication.projection.layerPins.filter(
    (pin) => pin.kind === 'transcription',
  )
  const sourcePin = transcriptionPins.find((pin) => pin.role === 'source')
    ?? transcriptionPins[0]
  const readingPin = transcriptionPins.find((pin) => pin.role === 'reading')
    ?? transcriptionPins[1]
    ?? transcriptionPins[0]
  const sourceLabel = sourcePin
    ? sourcePin.label + ' · ' + sourcePin.revision
    : 'Source transcription layer'
  const readingLabel = readingPin
    ? readingPin.label + ' · ' + readingPin.revision
    : 'Reading transcription layer'
  const differenceCount = props.payload.sourceRegions.filter(
    (region) => region.sourceText.trim() !== region.transcription.trim(),
  ).length
  return (
    <div className="reader-compare" id="reader-text">
      <header>
        <div><span>Compare pinned transcription layers</span><h2>{readingLabel} ↔ {sourceLabel}</h2></div>
        <Tag intent="warning" minimal>{differenceCount} differing regions</Tag>
      </header>
      {props.payload.sourceRegions.map((region) => (
        <section key={region.id}>
          <div><strong>{region.id}</strong><small>{region.label}</small></div>
          <p>
            <span>{sourcePin?.label ?? 'Source layer'}</span>
            <EntityLinkedText publication={props.publication} entityIds={region.entityIds} text={region.sourceText} />
          </p>
          <p>
            <span>{readingPin?.label ?? 'Reading layer'}</span>
            <EntityLinkedText publication={props.publication} entityIds={region.entityIds} text={region.transcription} />
          </p>
        </section>
      ))}
    </div>
  )
}

interface ReferencePresentationProps extends ReaderKernelPresentationProps {
  payload: ReferenceReaderPayload
}

const referencePresentationRegistry: Readonly<Record<
  string,
  ComponentType<ReferencePresentationProps>
>> = {
  reading: ReferenceReadingPresentation,
  explore: ReferenceExplorePresentation,
}

function ReferenceReadingPresentation(props: ReferencePresentationProps) {
  return (
    <ReadingLayout
      publication={props.publication}
      sections={props.payload.sections}
      showNotes={props.showNotes}
      deck="A continuous path through a synthetic multi-entry reference projection. Entry navigation remains available in Explore."
    />
  )
}

function ReferenceExplorePresentation(props: ReferencePresentationProps) {
  return (
    <div className="reader-explore" id="reader-text">
      <header>
        <span>{props.payload.volumeLabel}</span>
        <h2>Entries and cross-references</h2>
        <p>The reference adapter contributes real entry targets to shared Reader primitives.</p>
      </header>
      <div className="reader-explore__grid">
        {props.payload.entries.map((entry, index) => (
          <div
            key={entry.id}
            className={props.selectionId === entry.id ? 'reader-explore__entry is-active' : 'reader-explore__entry'}
          >
            <div className="reader-explore__content">
              <span className="reader-explore__figure is-entity">
                <i>{String(index + 1).padStart(2, '0')}</i>
                <Icon icon="diagram-tree" size={24} />
              </span>
              <span>
                <small>{entry.kicker}</small>
                <strong>
                  <EntityLinkedText publication={props.publication} entityIds={entry.entityIds} text={entry.label} />
                </strong>
                <p>
                  <EntityLinkedText publication={props.publication} entityIds={entry.entityIds} text={entry.summary} />
                </p>
              </span>
            </div>
            <div className="reader-explore__footer">
              <Button
                small
                minimal
                icon={props.selectionId === entry.id ? 'tick' : 'locate'}
                text={props.selectionId === entry.id ? 'Selected entry' : 'Select entry'}
                onClick={() => props.onSelect(entry.id)}
              />
              <CitationLinks publication={props.publication} citationIds={entry.citationIds} />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function ReadingLayout({
  publication,
  sections,
  showNotes,
  deck,
}: {
  publication: ReaderPublication
  sections: readonly ReaderSection[]
  showNotes: boolean
  deck: string
}) {
  return (
    <div className="reader-reading" id="reader-text">
      <aside className="reader-reading__contents">
        <strong>Contents</strong>
        {sections.map((section, index) => (
          <a key={section.id} href={'#' + section.id}>
            <span>{String(index + 1).padStart(2, '0')}</span>{section.heading}
          </a>
        ))}
        <small>{publication.languageLabel}</small>
      </aside>
      <div className="reader-reading__text">
        <p className="reader-deck">{deck}</p>
        {sections.map((section) => (
          <ReaderSectionBlock
            key={section.id}
            publication={publication}
            section={section}
            showNotes={showNotes}
          />
        ))}
      </div>
    </div>
  )
}

function ReaderSectionBlock({
  publication,
  section,
  showNotes,
}: {
  publication: ReaderPublication
  section: ReaderSection
  showNotes: boolean
}) {
  return (
    <section className="reader-section" id={section.id}>
      <h2>
        <EntityLinkedText publication={publication} entityIds={section.entityIds} text={section.heading} />
      </h2>
      {section.body.map((paragraph, index) => (
        <p key={index}>
          <EntityLinkedText publication={publication} entityIds={section.entityIds} text={paragraph} />
        </p>
      ))}
      {showNotes && section.note && (
        <aside className="reader-note">
          <Icon icon="annotation" />
          <span>
            <strong>Reader note</strong>
            <EntityLinkedText publication={publication} entityIds={section.entityIds} text={section.note} />
          </span>
        </aside>
      )}
      <div className="reader-inline-apparatus">
        <CitationLinks publication={publication} citationIds={section.citationIds} />
      </div>
    </section>
  )
}

function SourceLeaf({
  folio,
  regions,
  selectedRegionId,
  onSelectRegion,
}: {
  folio: string
  regions: ManuscriptReaderPayload['sourceRegions']
  selectedRegionId: string
  onSelectRegion: (id: string) => void
}) {
  return (
    <div className="reader-source-leaf" aria-label="Abstract facsimile placeholder">
      <div className="reader-source-leaf__paper">
        <span className="reader-source-leaf__folio">{folio}</span>
        {Array.from({ length: 21 }, (_, index) => <i key={index} />)}
        {regions.map((region, index) => (
          <button
            key={region.id}
            className={region.id === selectedRegionId ? 'is-active' : ''}
            style={{ top: 14 + index * 27 + '%', height: index === 0 ? '15%' : '21%' }}
            aria-label={'Select ' + region.label}
            onClick={() => onSelectRegion(region.id)}
          >
            <span>{region.id}</span>
          </button>
        ))}
      </div>
      <small>Abstract facsimile · source raster excluded from design fixture</small>
    </div>
  )
}

interface EntityMention {
  start: number
  end: number
  entity: ReaderEntity
}

function entityAnchorId(entityId: string) {
  return 'reader-entity-' + entityId.replace(/[^a-zA-Z0-9_-]/g, '-')
}

function isNameCharacter(value: string) {
  return value !== '' && /[\p{L}\p{N}_]/u.test(value)
}

function findEntityMentions(
  text: string,
  entities: readonly ReaderEntity[],
): EntityMention[] {
  const foldedText = text.toLocaleLowerCase()
  const candidates = entities.flatMap((entity) => {
    const seen = new Set<string>()
    return [entity.label, ...entity.writtenForms].flatMap((form) => {
      const foldedForm = form.trim().toLocaleLowerCase()
      if (!foldedForm || seen.has(foldedForm)) return []
      seen.add(foldedForm)
      return [{ entity, foldedForm }]
    })
  })
  const mentions: EntityMention[] = []
  let cursor = 0

  while (cursor < text.length) {
    let next: EntityMention | null = null
    for (const candidate of candidates) {
      let start = foldedText.indexOf(candidate.foldedForm, cursor)
      while (start >= 0) {
        const end = start + candidate.foldedForm.length
        const hasLeftBoundary = start === 0 || !isNameCharacter(text[start - 1] ?? '')
        const hasRightBoundary = end === text.length || !isNameCharacter(text[end] ?? '')
        if (hasLeftBoundary && hasRightBoundary) {
          if (
            !next
            || start < next.start
            || (start === next.start && end - start > next.end - next.start)
          ) {
            next = { start, end, entity: candidate.entity }
          }
          break
        }
        start = foldedText.indexOf(candidate.foldedForm, start + 1)
      }
    }
    if (!next) break
    mentions.push(next)
    cursor = next.end
  }

  return mentions
}

function EntityLinkedText({
  publication,
  entityIds,
  text,
}: {
  publication: ReaderPublication
  entityIds: readonly string[]
  text: string
}) {
  const entityIdSet = new Set(entityIds)
  const entities = publication.entities.filter((entity) => entityIdSet.has(entity.id))
  const mentions = findEntityMentions(text, entities)
  if (mentions.length === 0) return <>{text}</>

  const parts: ReactNode[] = []
  let cursor = 0
  mentions.forEach((mention, index) => {
    if (mention.start > cursor) parts.push(text.slice(cursor, mention.start))
    const literal = text.slice(mention.start, mention.end)
    parts.push(
      <a
        key={mention.entity.id + ':' + mention.start + ':' + index}
        className="reader-entity-link"
        href={'#' + entityAnchorId(mention.entity.id)}
        data-entity-id={mention.entity.id}
        data-entity-release={publication.projection.entityReleaseId ?? 'unreleased'}
        title={mention.entity.label + ' · ' + mention.entity.authorityState + ' · internal projection link'}
        aria-label={literal + ': ' + mention.entity.label + ' entity'}
      >
        {literal}
      </a>,
    )
    cursor = mention.end
  })
  if (cursor < text.length) parts.push(text.slice(cursor))
  return <>{parts}</>
}

function EntityApparatus({ publication }: { publication: ReaderPublication }) {
  if (publication.entities.length === 0) return null
  return (
    <section className="reader-entity-apparatus" id="reader-entities" aria-labelledby="reader-entities-title">
      <header>
        <span>
          <strong id="reader-entities-title">Plant entities</strong>
          <small>Internal targets from the projection-pinned entity layer</small>
        </span>
        <Tag minimal icon="diagram-tree">
          {publication.projection.entityReleaseId ?? 'No entity release'}
        </Tag>
      </header>
      <div className="reader-entity-apparatus__grid">
        {publication.entities.map((entity) => (
          <article
            key={entity.id}
            id={entityAnchorId(entity.id)}
            className="reader-entity-record"
            tabIndex={-1}
          >
            <span className="reader-entity-record__state">{entity.authorityState}</span>
            <h2>{entity.label}</h2>
            <p>{entity.description}</p>
            <small><strong>Written forms</strong> {entity.writtenForms.join(' · ')}</small>
          </article>
        ))}
      </div>
      <small className="reader-entity-apparatus__notice">
        Same-document projection links only · no external authority URL asserted
      </small>
    </section>
  )
}

function CitationLinks({
  publication,
  citationIds,
}: {
  publication: ReaderPublication
  citationIds: readonly string[]
}) {
  if (citationIds.length === 0) return null
  const citationById = new Map(publication.citations.map((citation) => [citation.id, citation]))
  return (
    <span className="reader-citation-links">
      {citationIds.map((id, index) => (
        <a key={id} href={'#' + id} aria-label={citationById.get(id)?.label}>
          [{index + 1}]
        </a>
      ))}
    </span>
  )
}

function CitationPanel({ publication }: { publication: ReaderPublication }) {
  return (
    <Card className="reader-citations" id="reader-citation">
      <span>Citation preview · not published</span>
      {publication.citations.map((citation) => (
        <p key={citation.id} id={citation.id}>
          <strong>{citation.label}</strong>{citation.text}
        </p>
      ))}
      <small>Projection fixture: {publication.projectionId}</small>
    </Card>
  )
}
