import { useEffect, useMemo, useState } from "react";

/**
 * Dashboard (desired-only):
 * - Shows ONLY the desired scraped lists:
 *   1) CBP CSMS messages (recent)
 *   2) WhiteHouse Fact Sheets matching "Tariff"
 *   3) CBP Documents Library matching "tariff"
 *   4) Federal Register documents matching "Tariff Rates" sorted newest
 *
 * UI rules:
 * - Consistent ISO dates (YYYY-MM-DD) everywhere
 * - No scrollbars inside cards
 * - Anything beyond card limit is in View More (modal may scroll)
 * - CSMS titles: remove "CSMS #67735996 - " prefix
 * - CSMS left column: show ONLY date (no CSMS #)
 * - Links should always work: support `link` or `url`
 */

function domainFromUrl(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "source";
  }
}

function clampText(s, maxChars) {
  const t = (s || "").replace(/\s+/g, " ").trim();
  if (t.length <= maxChars) return t;
  return t.slice(0, maxChars).trim() + "…";
}

function pad2(n) {
  return String(n).padStart(2, "0");
}

/** Normalize multiple formats to YYYY-MM-DD */
function toISODate(value) {
  const s = (value || "").toString().trim();
  if (!s) return "";

  // Already ISO: 2026-02-12
  const iso = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (iso) return `${iso[1]}-${iso[2]}-${iso[3]}`;

  // US numeric: 02/12/2026 ...
  const us = s.match(/^(\d{2})\/(\d{2})\/(\d{4})/);
  if (us) return `${us[3]}-${us[1]}-${us[2]}`;

  // Month name: February 9, 2026  (or February 9 2026)
  const m = s.match(
    /^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,)?\s+(\d{4})/i
  );
  if (m) {
    const months = {
      january: 1,
      february: 2,
      march: 3,
      april: 4,
      may: 5,
      june: 6,
      july: 7,
      august: 8,
      september: 9,
      october: 10,
      november: 11,
      december: 12,
    };
    const mm = pad2(months[m[1].toLowerCase()] || 0);
    const dd = pad2(parseInt(m[2], 10));
    return `${m[3]}-${mm}-${dd}`;
  }

  return s; // fallback
}

/** Remove CSMS prefix patterns from title text */
function stripCsmsPrefix(title) {
  const t = (title || "").replace(/\s+/g, " ").trim();
  if (!t) return "";

  // Handles:
  // "CSMS # 67735996 - blah"
  // "CSMS #67735996 - blah"
  // "CSMS # 67735996: blah"
  // "CSMS #67735996 — blah"
  return t.replace(/^CSMS\s*#?\s*\d+\s*[-–—:]\s*/i, "");
}

/** Return a clickable href reliably */
function resolveHref(a) {
  const href = (a?.link || a?.url || "").trim();
  return href || "";
}

function isDesiredSource(url) {
  const u = (url || "").toLowerCase();
  return (
    u.includes("cbp.gov/trade/automated/cargo-systems-messaging-service") ||
    u.replace(/\/$/, "") === "https://www.whitehouse.gov/fact-sheets" ||
    u.replace(/\/$/, "") === "https://www.cbp.gov/documents-library" ||
    (u.includes("federalregister.gov") && u.includes("/documents/search"))
  );
}

/** Meaningful card headings (no "Top 5 newest") */
function sourceLabel(item) {
  const u = (item?.url || "").toLowerCase();
  if (u.includes("cbp.gov/trade/automated/cargo-systems-messaging-service"))
    return "CBP CSMS — Recent Messages";
  if (u.replace(/\/$/, "") === "https://www.whitehouse.gov/fact-sheets")
    return 'White House — Fact Sheets matching "Tariff"';
  if (u.replace(/\/$/, "") === "https://www.cbp.gov/documents-library")
    return 'CBP — Documents matching "tariff"';
  if (u.includes("federalregister.gov") && u.includes("/documents/search"))
    return 'Federal Register — Results for "Tariff Rates" (Newest first)';
  return domainFromUrl(item?.url);
}

/** Table column headings per source */
function tableHeadings(item) {
  const u = (item?.url || "").toLowerCase();
  if (u.includes("federalregister.gov") && u.includes("/documents/search")) {
    return { c1: "Publication date", c2: "Document title" };
  }
  if (u.replace(/\/$/, "") === "https://www.whitehouse.gov/fact-sheets") {
    return { c1: "Date", c2: "Fact sheet" };
  }
  if (u.replace(/\/$/, "") === "https://www.cbp.gov/documents-library") {
    return { c1: "Date", c2: "Document" };
  }
  return { c1: "Date", c2: "Title" };
}

function Modal({ open, title, subtitle, children, onClose }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => e.key === "Escape" && onClose?.();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="mOverlay" onMouseDown={onClose}>
      <div className="mCard" onMouseDown={(e) => e.stopPropagation()}>
        <div className="mHeader">
          <div>
            <div className="mTitle">{title}</div>
            {subtitle ? <div className="mSub">{subtitle}</div> : null}
          </div>
          <button className="iconBtn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="mBody">{children}</div>
      </div>
    </div>
  );
}

function RowLink({ href, children }) {
  if (!href) return <span>{children}</span>;
  return (
    <a className="rowLink" href={href} target="_blank" rel="noreferrer">
      {children}
    </a>
  );
}

/** Desired-only renderer (uses announcements only). */
function DesiredContent({ item, compact = true }) {
  const anns = Array.isArray(item?.announcements) ? item.announcements : [];
  if (!anns.length) return <div className="empty">No structured items found.</div>;

  const isCSMS = (item?.url || "")
    .toLowerCase()
    .includes("cbp.gov/trade/automated/cargo-systems-messaging-service");

  // No scrollbars in cards: show subset, rest in modal.
  const cardLimit = isCSMS ? 6 : 5;
  const shown = compact ? anns.slice(0, cardLimit) : anns;

  if (isCSMS) {
    return (
      <div>
        <ul className="lst">
          {shown.map((a, i) => {
            const rawTitle = (a?.title || "").trim();
            const cleanTitle = stripCsmsPrefix(rawTitle);
            const pubRaw = (a?.published || a?.date || "").trim();
            const pub = toISODate(pubRaw);
            const link = resolveHref(a);

            // ✅ left column: date only (no CSMS #)
            const left = compact ? clampText(pub, 10) : pub;
            const right = compact ? clampText(cleanTitle, 140) : cleanTitle;

            return (
              <li key={i} className="row">
                <div className="rowMeta" title={pub}>{left || "—"}</div>
                <div className="rowMain" title={cleanTitle}>
                  <RowLink href={link}>{right || "Untitled"}</RowLink>
                </div>
              </li>
            );
          })}
        </ul>

        {compact && anns.length > shown.length ? (
          <div className="moreHint">More items in “View More”</div>
        ) : null}
      </div>
    );
  }

  const { c1, c2 } = tableHeadings(item);

  return (
    <div>
      <table className="tbl">
        <thead>
          <tr>
            <th>{c1}</th>
            <th>{c2}</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((a, i) => {
            const dateRaw = (a?.date || a?.published || "").trim();
            const date = toISODate(dateRaw);
            const title = (a?.title || a?.announcement || "").trim();
            const link = resolveHref(a);

            return (
              <tr key={i}>
                <td className="tdDate" title={date}>
                  {compact ? clampText(date, 10) : date}
                </td>
                <td title={title}>
                  <div className="cellWrap">
                    <RowLink href={link}>
                      {compact ? clampText(title, 160) : title}
                    </RowLink>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {compact && anns.length > shown.length ? (
        <div className="moreHint">More items in “View More”</div>
      ) : null}
    </div>
  );
}

function SourceCard({ item, onOpen }) {
  const host = useMemo(() => domainFromUrl(item.url), [item.url]);
  const label = sourceLabel(item);

  return (
    <div className="card">
      <div className="cardTop">
        <div className="pill" title={host}>{host}</div>
        <div className="cardLinks">
          <button className="linkBtn" onClick={() => onOpen(item)}>
            View More
          </button>
          <a className="linkBtn" href={item.url} target="_blank" rel="noreferrer">
            Source
          </a>
        </div>
      </div>

      <div className="cardTitle" title={label}>
        {clampText(label, 80)}
      </div>

      <div className="cardBody">
        <DesiredContent item={item} compact />
      </div>
    </div>
  );
}

export default function App() {
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [data, setData] = useState({ count: 0, results: [], errors: [] });
  const [selected, setSelected] = useState(null);

  async function runScrapeAll() {
    setLoading(true);
    setErr("");
    try {
      const res = await fetch("http://localhost:4000/api/scrape-all");
      if (!res.ok) {
        const t = await res.text();
        throw new Error(`HTTP ${res.status}: ${t}`);
      }
      const json = await res.json();
      setData(json);
    } catch (e) {
      setErr(e?.message || "Failed to fetch");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    runScrapeAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const rawResults = data?.results || [];
  const results = rawResults
    .filter((r) => isDesiredSource(r?.url))
    .filter((r) => Array.isArray(r?.announcements) && r.announcements.length > 0);

  const errors = data?.errors || [];

  return (
    <div className="wrap">
      <div className="top">
        <div className="brand">
          <div className="logo">UPS</div>
          <div>
            <div className="h1">Tariff News</div>
            <div className="sub">CSMS + Tariff searches</div>
          </div>
        </div>

        <button className="primary" onClick={runScrapeAll} disabled={loading}>
          {loading ? "Running…" : "Run scraping"}
        </button>
      </div>

      {err ? (
        <div className="alert error">
          <div className="alertTitle">Error</div>
          <div className="alertText">{err}</div>
        </div>
      ) : null}

      {errors.length ? (
        <div className="alert warn">
          <div className="alertTitle">Some sources failed</div>
          <div className="alertText">Showing first {Math.min(5, errors.length)} errors:</div>
          <ul className="errList">
            {errors.slice(0, 5).map((e, i) => (
              <li key={i}>
                <span className="mono">{e.url}</span>
                <div className="muted">{e.error}</div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="grid">
        {results.map((item) => (
          <SourceCard key={item.url} item={item} onOpen={setSelected} />
        ))}
      </div>

      <Modal
        open={!!selected}
        title={sourceLabel(selected)}
        subtitle={selected?.url}
        onClose={() => setSelected(null)}
      >
        {selected ? (
          <div className="modalContent">
            <div className="modalActions">
              <a className="linkBtn" href={selected.url} target="_blank" rel="noreferrer">
                Open Source
              </a>
            </div>
            <DesiredContent item={selected} compact={false} />
          </div>
        ) : null}
      </Modal>

      <style>{`
        :root{
          --bg:#f4f6fb;
          --card:#ffffff;
          --border:rgba(15,23,42,.12);
          --text:#0f172a;
          --muted:rgba(15,23,42,.65);
          --muted2:rgba(15,23,42,.5);
          --shadow:0 10px 30px rgba(15,23,42,.08);
          --radius:14px;
          --primary:#3b82f6;
          --primary2:#2563eb;
        }
        *{box-sizing:border-box}
        body{margin:0;background:var(--bg);color:var(--text);
             font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial}
        .wrap{max-width:1100px;margin:0 auto;padding:28px 18px 42px}
        .top{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:22px}
        .brand{display:flex;align-items:center;gap:12px}
        .logo{width:42px;height:42px;border-radius:12px;display:grid;place-items:center;
              background:linear-gradient(135deg,#60a5fa,#a78bfa);color:#fff;font-weight:800}
        .h1{font-size:22px;font-weight:800;line-height:1}
        .sub{margin-top:4px;color:var(--muted);font-size:13px}
        .primary{border:none;border-radius:12px;padding:10px 14px;font-weight:700;color:#fff;
                 background:linear-gradient(135deg,var(--primary),#7c3aed);
                 box-shadow:0 12px 24px rgba(59,130,246,.18);cursor:pointer}
        .primary:disabled{opacity:.6;cursor:not-allowed}

        .alert{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
               padding:14px 16px;margin-bottom:14px;box-shadow:var(--shadow)}
        .alertTitle{font-weight:800;margin-bottom:6px}
        .alertText{color:var(--muted);font-size:13px}
        .alert.error{border-color:rgba(239,68,68,.25)}
        .alert.warn{border-color:rgba(245,158,11,.28)}
        .errList{margin:10px 0 0;padding-left:18px;color:var(--muted);font-size:13px}
        .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace}
        .muted{color:var(--muted2);font-size:12px;margin-top:2px}

        .grid{display:grid;grid-template-columns:repeat(2, minmax(0, 1fr));gap:16px;margin-top:18px}
        @media (max-width: 860px){.grid{grid-template-columns:1fr}}

        .card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
              box-shadow:var(--shadow);padding:14px 14px 12px}
        .cardTop{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}
        .pill{font-size:12px;color:var(--muted);border:1px solid var(--border);padding:6px 10px;
              border-radius:999px;background:rgba(15,23,42,.02)}
        .cardLinks{display:flex;gap:8px;align-items:center}
        .linkBtn{border:none;background:transparent;color:var(--primary2);font-weight:700;
                 font-size:13px;cursor:pointer;padding:4px 6px;text-decoration:none}
        .linkBtn:hover{text-decoration:underline}
        .cardTitle{font-weight:900;font-size:14px;line-height:1.25;margin:6px 0 10px}
        .cardBody{border:1px solid var(--border);border-radius:12px;padding:10px;
                  background:rgba(15,23,42,.02);}

        .empty{color:var(--muted);font-size:13px}
        .moreHint{margin-top:10px;color:rgba(15,23,42,.55);font-size:12px;font-weight:700}

        /* CSMS list rows (no scrollbars) */
        .lst{margin:0;padding-left:18px;color:rgba(15,23,42,.82);font-size:13px}
        .lst li{margin:8px 0;word-break:break-word;line-height:1.25}
        .row{display:grid;grid-template-columns:110px 1fr;gap:10px;align-items:start}
        @media (max-width: 540px){.row{grid-template-columns:1fr}}
        .rowMeta{color:rgba(15,23,42,.65);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
        .rowMain{font-size:13px}
        .rowLink{color:var(--primary2);text-decoration:none;font-weight:800}
        .rowLink:hover{text-decoration:underline}

        /* tables (no scrollbars in cards) */
        .tbl{width:100%;border-collapse:collapse;font-size:13px}
        .tbl th,.tbl td{border:1px solid rgba(15,23,42,.10);padding:8px 10px;text-align:left;vertical-align:top}
        .tbl th{font-weight:900;color:rgba(15,23,42,.8);background:#fff}
        .cellWrap{word-break:break-word;white-space:normal;line-height:1.25}
        .tdDate{white-space:nowrap;min-width:110px}

        /* modal */
        .mOverlay{position:fixed;inset:0;background:rgba(2,6,23,.40);display:flex;align-items:center;justify-content:center;padding:18px}
        .mCard{width:min(980px, 96vw);max-height:88vh;overflow:auto;background:var(--card);
               border:1px solid var(--border);border-radius:18px;box-shadow:0 30px 70px rgba(2,6,23,.25)}
        .mHeader{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;
                 padding:16px 16px 10px;border-bottom:1px solid var(--border)}
        .mTitle{font-weight:900;font-size:16px}
        .mSub{color:var(--muted);font-size:13px;margin-top:4px;word-break:break-all}
        .iconBtn{border:1px solid var(--border);background:#fff;border-radius:10px;width:36px;height:36px;cursor:pointer}
        .mBody{padding:16px}
        .modalActions{display:flex;justify-content:flex-end;margin-bottom:12px}
      `}</style>
    </div>
  );
}
