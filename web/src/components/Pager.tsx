import { useEffect, useState } from "react";

/*
 * One pager for every table and list: "1–10 of 57", page numbers, and how many rows a page shows
 * (10, 20 or 50, remembered per list in this browser). Long pages that scroll forever are hard to
 * read on a NOC screen; ten rows is the default.
 */

export const PAGE_SIZES = [10, 20, 50] as const;

function stored(key: string): number {
  try {
    const n = Number(window.localStorage.getItem(`forgesre.pageSize.${key}`));
    return (PAGE_SIZES as readonly number[]).includes(n) ? n : 10;
  } catch {
    return 10; // storage can be off in some browsers; the default is fine
  }
}

/** Page size of one list (10 by default), remembered in this browser under ``key``. */
export function usePageSize(key: string): [number, (n: number) => void] {
  const [size, setSize] = useState(() => stored(key));
  const set = (n: number) => {
    setSize(n);
    try {
      window.localStorage.setItem(`forgesre.pageSize.${key}`, String(n));
    } catch {
      /* not remembered, still applied */
    }
  };
  return [size, set];
}

/** Page numbers to show: first, last, and two around the current one, with gaps as null. */
function pages(current: number, count: number): (number | null)[] {
  const keep = new Set([0, count - 1, current - 1, current, current + 1].filter((p) => p >= 0 && p < count));
  const out: (number | null)[] = [];
  [...keep].sort((a, b) => a - b).forEach((p, i, all) => {
    if (i && p - all[i - 1] > 1) out.push(null);
    out.push(p);
  });
  return out;
}

export function Pager({ total, offset, size, onOffset, onSize, label = "rows" }: {
  total: number; offset: number; size: number; onOffset: (n: number) => void; onSize: (n: number) => void; label?: string;
}) {
  if (total <= PAGE_SIZES[0]) return null;
  const count = Math.max(1, Math.ceil(total / size));
  const current = Math.min(count - 1, Math.floor(offset / size));
  return (
    <nav className="pager" aria-label={`Pages of ${label}`}>
      <span className="small faint">{offset + 1}–{Math.min(total, offset + size)} of {total}</span>
      <span className="row" style={{ gap: 4 }}>
        <button className="btn" type="button" disabled={current === 0} onClick={() => onOffset((current - 1) * size)} aria-label="Previous page">‹</button>
        {pages(current, count).map((p, i) => (p === null ? <span key={`gap${i}`} className="faint">…</span> : (
          <button key={p} type="button" className={`btn${p === current ? " primary" : ""}`} aria-current={p === current ? "page" : undefined}
            onClick={() => onOffset(p * size)}>{p + 1}</button>
        )))}
        <button className="btn" type="button" disabled={current >= count - 1} onClick={() => onOffset((current + 1) * size)} aria-label="Next page">›</button>
      </span>
      <label className="row small faint" style={{ gap: 6 }}>
        Per page
        <select className="search" value={size} onChange={(e) => (onSize(Number(e.target.value)), onOffset(0))} aria-label={`${label} per page`} style={{ minWidth: 0, width: "auto" }}>
          {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
      </label>
    </nav>
  );
}

/** Client-side paging of a list that is already loaded: the rows of this page and the pager. */
export function usePaged<T>(rows: T[], key: string, label = "rows") {
  const [size, setSize] = usePageSize(key);
  const [offset, setOffset] = useState(0);
  // a filter that shrinks the list must not leave you on an empty page
  useEffect(() => {
    if (offset >= rows.length && offset > 0) setOffset(0);
  }, [rows.length, offset]);
  return {
    page: rows.slice(offset, offset + size),
    pager: <Pager total={rows.length} offset={offset} size={size} onOffset={setOffset} onSize={setSize} label={label} />,
    reset: () => setOffset(0),
  };
}
