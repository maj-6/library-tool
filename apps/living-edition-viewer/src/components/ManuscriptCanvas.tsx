import { useMemo, useRef, useState } from 'react'
import { Button, ButtonGroup, Icon, Tag } from '@blueprintjs/core'
import { activeManuscript } from '../data/registries'
import type { DrawMode, Point, Region, RegionType } from '../types'

interface Props {
  regions: Region[]
  selectedId: string | null
  drawMode: DrawMode
  activeTypeId: string
  regionTypes: RegionType[]
  showRegions: boolean
  onSelect: (id: string | null) => void
  onChangeDrawMode: (mode: DrawMode) => void
  onAddRegion: (region: Region) => void
  compact?: boolean
  crop?: boolean
}

interface DraftRect { start: Point; end: Point }

function percentPoint(event: React.PointerEvent<SVGSVGElement>): Point {
  const bounds = event.currentTarget.getBoundingClientRect()
  return {
    x: Math.max(0, Math.min(100, ((event.clientX - bounds.left) / bounds.width) * 100)),
    y: Math.max(0, Math.min(100, ((event.clientY - bounds.top) / bounds.height) * 100)),
  }
}

export function ManuscriptCanvas({
  regions,
  selectedId,
  drawMode,
  activeTypeId,
  regionTypes,
  showRegions,
  onSelect,
  onChangeDrawMode,
  onAddRegion,
  compact = false,
  crop = false,
}: Props) {
  const [draftRect, setDraftRect] = useState<DraftRect | null>(null)
  const [draftPolygon, setDraftPolygon] = useState<Point[]>([])
  const counter = useRef(1)
  const activeType = regionTypes.find((type) => type.id === activeTypeId) ?? regionTypes[0]

  const normalizedDraft = useMemo(() => {
    if (!draftRect) return null
    return {
      x: Math.min(draftRect.start.x, draftRect.end.x),
      y: Math.min(draftRect.start.y, draftRect.end.y),
      width: Math.abs(draftRect.start.x - draftRect.end.x),
      height: Math.abs(draftRect.start.y - draftRect.end.y),
    }
  }, [draftRect])

  const finishPolygon = () => {
    if (draftPolygon.length < 3) return
    const xs = draftPolygon.map((point) => point.x)
    const ys = draftPolygon.map((point) => point.y)
    const x = Math.min(...xs)
    const y = Math.min(...ys)
    const width = Math.max(...xs) - x
    const height = Math.max(...ys) - y
    onAddRegion({
      id: `manual-poly-${Date.now()}-${counter.current++}`,
      label: `New ${activeType.name}`,
      typeId: activeType.id,
      color: activeType.color,
      x,
      y,
      width,
      height,
      polygon: draftPolygon.map((point) => ({ x: point.x, y: point.y })),
      source: 'manual',
    })
    setDraftPolygon([])
    onChangeDrawMode('select')
  }

  const handlePointerDown = (event: React.PointerEvent<SVGSVGElement>) => {
    if (drawMode === 'rectangle') {
      event.currentTarget.setPointerCapture(event.pointerId)
      const point = percentPoint(event)
      setDraftRect({ start: point, end: point })
      onSelect(null)
    }
  }

  const handlePointerMove = (event: React.PointerEvent<SVGSVGElement>) => {
    if (drawMode === 'rectangle' && draftRect) {
      setDraftRect({ ...draftRect, end: percentPoint(event) })
    }
  }

  const handlePointerUp = (event: React.PointerEvent<SVGSVGElement>) => {
    if (drawMode !== 'rectangle' || !draftRect || !normalizedDraft) return
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    if (normalizedDraft.width > 1 && normalizedDraft.height > 1) {
      onAddRegion({
        id: `manual-box-${Date.now()}-${counter.current++}`,
        label: `New ${activeType.name}`,
        typeId: activeType.id,
        color: activeType.color,
        ...normalizedDraft,
        source: 'manual',
      })
      onChangeDrawMode('select')
    }
    setDraftRect(null)
  }

  const handleCanvasClick = (event: React.MouseEvent<SVGSVGElement>) => {
    if (drawMode === 'select') onSelect(null)
    if (drawMode === 'polygon') {
      const bounds = event.currentTarget.getBoundingClientRect()
      setDraftPolygon((current) => [...current, {
        x: ((event.clientX - bounds.left) / bounds.width) * 100,
        y: ((event.clientY - bounds.top) / bounds.height) * 100,
      }])
    }
  }

  return (
    <div className={`manuscript-canvas ${compact ? 'is-compact' : ''} ${crop ? 'is-crop' : ''}`}>
      {!compact && (
        <div className="canvas-toolbar">
          <ButtonGroup minimal>
            <Button icon="hand" small active={drawMode === 'select'} onClick={() => onChangeDrawMode('select')}>Select</Button>
            <Button icon="selection" small active={drawMode === 'rectangle'} onClick={() => onChangeDrawMode('rectangle')}>Box</Button>
            <Button icon="polygon-filter" small active={drawMode === 'polygon'} onClick={() => onChangeDrawMode('polygon')}>Polygon</Button>
          </ButtonGroup>
          <span className="canvas-toolbar__spacer" />
          <Tag minimal icon="new-object">{activeType.name}</Tag>
          <Button minimal small icon="zoom-in" aria-label="Zoom in" />
          <Button minimal small icon="reset" aria-label="Fit page" />
        </div>
      )}
      <div className="canvas-stage">
        <div className="page-image-wrap" style={{ aspectRatio: `${activeManuscript.width} / ${activeManuscript.height}` }}>
          <div
            className="mock-manuscript-page"
            role="img"
            aria-label={`Abstract placeholder for ${activeManuscript.alt}; source raster is loaded only from a local edition`}
          >
            <span className="mock-rubric">¶ De virtutibus herbarum</span>
            {Array.from({ length: 30 }, (_, index) => (
              <i key={index} style={{ width: `${72 + ((index * 17) % 23)}%` }} />
            ))}
          </div>
          <svg
            className={`region-overlay is-${drawMode}`}
            viewBox="0 0 100 100"
            preserveAspectRatio="none"
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerUp}
            onClick={handleCanvasClick}
            onDoubleClick={(event) => { event.preventDefault(); if (drawMode === 'polygon') finishPolygon() }}
          >
            {showRegions && regions.map((region) => {
              const selected = selectedId === region.id
              if (region.polygon) {
                return (
                  <g key={region.id} onClick={(event) => { if (drawMode === 'select') { event.stopPropagation(); onSelect(region.id) } }}>
                    <polygon
                      points={region.polygon.map((point) => `${point.x},${point.y}`).join(' ')}
                      className={selected ? 'region-shape is-selected' : 'region-shape'}
                      style={{ '--region-color': region.color } as React.CSSProperties}
                    />
                  </g>
                )
              }
              return (
                <g key={region.id} onClick={(event) => { if (drawMode === 'select') { event.stopPropagation(); onSelect(region.id) } }}>
                  <rect
                    x={region.x}
                    y={region.y}
                    width={region.width}
                    height={region.height}
                    className={selected ? 'region-shape is-selected' : 'region-shape'}
                    style={{ '--region-color': region.color } as React.CSSProperties}
                  />
                  <text x={region.x + 0.8} y={region.y + 2.2} className="region-label" style={{ '--region-color': region.color } as React.CSSProperties}>
                    {region.label}
                  </text>
                  {selected && <>
                    <circle cx={region.x} cy={region.y} r="0.6" className="region-handle" />
                    <circle cx={region.x + region.width} cy={region.y} r="0.6" className="region-handle" />
                    <circle cx={region.x} cy={region.y + region.height} r="0.6" className="region-handle" />
                    <circle cx={region.x + region.width} cy={region.y + region.height} r="0.6" className="region-handle" />
                  </>}
                </g>
              )
            })}
            {normalizedDraft && <rect className="region-draft" {...normalizedDraft} style={{ '--region-color': activeType.color } as React.CSSProperties} />}
            {draftPolygon.length > 0 && <>
              <polyline className="region-draft" points={draftPolygon.map((point) => `${point.x},${point.y}`).join(' ')} style={{ '--region-color': activeType.color } as React.CSSProperties} />
              {draftPolygon.map((point, index) => <circle key={index} cx={point.x} cy={point.y} r="0.65" className="region-draft-point" />)}
            </>}
          </svg>
          {drawMode !== 'select' && (
            <div className="draw-hint">
              <Icon icon={drawMode === 'rectangle' ? 'selection' : 'polygon-filter'} size={12} />
              {drawMode === 'rectangle' ? 'Drag to draw a region' : 'Click vertices · double-click to finish'}
              {draftPolygon.length >= 3 && <Button small intent="primary" onClick={(event) => { event.stopPropagation(); finishPolygon() }}>Finish</Button>}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
