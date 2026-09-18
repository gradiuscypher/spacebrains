import { useEffect, useState } from 'react'
import type { ModelInfo, SchemaField } from '../api'

type Values = Record<string, unknown>

interface Props {
  schema: SchemaField[]
  values: Values
  /** Fields that offer an OpenRouter model picker. */
  modelFields?: string[]
  models?: ModelInfo[]
  /** When true, empty inputs mean "inherit" (null) instead of the default. */
  nullable?: boolean
  inherited?: Values
  onSave: (patch: Values) => Promise<void>
}

export function SettingsForm({ schema, values, modelFields = [], models = [], nullable, inherited, onSave }: Props) {
  const [draft, setDraft] = useState<Values>(values)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  useEffect(() => setDraft(values), [values])

  const dirty = Object.keys(draft).filter((k) => JSON.stringify(draft[k]) !== JSON.stringify(values[k]))

  const save = async () => {
    setSaving(true)
    setMsg(null)
    try {
      const patch: Values = {}
      for (const k of dirty) patch[k] = draft[k]
      await onSave(patch)
      setMsg('Saved.')
    } catch (e) {
      setMsg(`Error: ${(e as Error).message}`)
    } finally {
      setSaving(false)
    }
  }

  const set = (k: string, v: unknown) => setDraft((d) => ({ ...d, [k]: v }))

  return (
    <div className="form">
      {schema.map((f) => {
        const v = draft[f.name]
        const isModel = modelFields.includes(f.name)
        const placeholder = nullable && inherited ? `inherit (${String(inherited[f.name])})` : undefined
        let input: React.ReactNode
        if (f.type === 'boolean') {
          input = nullable ? (
            <select value={v === null || v === undefined ? '' : String(v)} onChange={(e) => set(f.name, e.target.value === '' ? null : e.target.value === 'true')}>
              <option value="">inherit ({String(inherited?.[f.name])})</option>
              <option value="true">on</option>
              <option value="false">off</option>
            </select>
          ) : (
            <input type="checkbox" checked={Boolean(v)} onChange={(e) => set(f.name, e.target.checked)} />
          )
        } else if (f.enum) {
          input = (
            <select value={v === null || v === undefined ? '' : String(v)} onChange={(e) => set(f.name, e.target.value === '' ? null : e.target.value)}>
              {nullable && <option value="">inherit ({String(inherited?.[f.name])})</option>}
              {f.enum.map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          )
        } else if (isModel) {
          input = (
            <>
              <input type="text" list={`models-${f.name}`} value={(v as string) ?? ''} placeholder={placeholder} onChange={(e) => set(f.name, e.target.value === '' && nullable ? null : e.target.value)} />
              <datalist id={`models-${f.name}`}>
                {models.map((m) => (
                  <option key={m.id} value={m.id}>{`$${m.prompt_per_m}/M in · $${m.completion_per_m}/M out`}</option>
                ))}
              </datalist>
            </>
          )
        } else if (f.type === 'integer' || f.type === 'number') {
          input = (
            <input
              type="number"
              value={v === null || v === undefined ? '' : (v as number)}
              placeholder={placeholder}
              min={f.minimum ?? undefined}
              max={f.maximum ?? undefined}
              step={f.type === 'integer' ? 1 : 'any'}
              onChange={(e) => set(f.name, e.target.value === '' ? (nullable ? null : f.default) : Number(e.target.value))}
            />
          )
        } else if (f.name === 'operator_notes') {
          input = <textarea rows={4} value={(v as string) ?? ''} onChange={(e) => set(f.name, e.target.value)} />
        } else {
          input = <input type="text" value={(v as string) ?? ''} placeholder={placeholder} onChange={(e) => set(f.name, e.target.value === '' && nullable ? null : e.target.value)} />
        }
        return (
          <div className="field" key={f.name}>
            <label htmlFor={f.name}>{f.name.replaceAll('_', ' ')}</label>
            <div>
              {input}
              {f.description && <div className="help">{f.description}</div>}
            </div>
          </div>
        )
      })}
      <div className="row">
        <button className="primary" disabled={saving || dirty.length === 0} onClick={() => void save()}>
          Save {dirty.length ? `(${dirty.length})` : ''}
        </button>
        <button disabled={dirty.length === 0} onClick={() => setDraft(values)}>
          Reset
        </button>
        {msg && <span className={msg.startsWith('Error') ? 'pill bad' : 'pill good'}>{msg}</span>}
      </div>
    </div>
  )
}
