import { useEffect, useRef } from 'react'
import hljs from 'highlight.js/lib/core'
import sql from 'highlight.js/lib/languages/sql'

hljs.registerLanguage('sql', sql)

export function Sql({ code, className = '' }: { code: string; className?: string }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const html = hljs.highlight(code, { language: 'sql' }).value

  const open = () => {
    document.body.style.overflow = 'hidden'
    dialog.current?.showModal()
  }
  const close = () => {
    try { dialog.current?.close() } finally { document.body.style.overflow = '' }
  }

  useEffect(() => () => { document.body.style.overflow = '' }, [])

  return (
    <>
      <div className="relative group">
        <pre className={`hljs text-xs p-3 overflow-x-auto text-left ${className}`}>
          <code dangerouslySetInnerHTML={{ __html: html }} />
        </pre>
        <button type="button" onClick={open} aria-label="Expand SQL"
          className="absolute top-2 right-2 rounded bg-neutral-800/90 border border-neutral-700 px-2 py-1 text-xs text-neutral-300 opacity-70 hover:opacity-100 focus:opacity-100">
          ⛶
        </button>
      </div>
      <dialog ref={dialog} aria-modal="true"
        onCancel={(e) => { e.preventDefault(); close() }}
        onKeyDown={(e) => { if (e.key === 'Escape') close() }}
        onClick={(e) => { if (e.target === e.currentTarget) close() }}
        className="m-auto w-fit min-w-[min(36rem,92vw)] max-w-[92vw] max-w-6xl max-h-[86vh] p-0 rounded-xl border border-neutral-700 bg-neutral-950 text-neutral-200 backdrop:bg-black/75">
        <div className="flex items-center justify-between border-b border-neutral-800 px-4 py-3">
          <span className="text-xs uppercase tracking-wide text-neutral-500">SQL</span>
          <button type="button" onClick={close} aria-label="Close SQL" className="text-neutral-400 hover:text-white text-xl">×</button>
        </div>
        <pre className="hljs text-sm p-5 overflow-auto text-left w-fit min-w-full max-w-[calc(92vw-2px)] max-h-[calc(86vh-3.5rem)] m-0">
          <code dangerouslySetInnerHTML={{ __html: html }} />
        </pre>
      </dialog>
    </>
  )
}
