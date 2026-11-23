import { useEffect, useState } from "react";

// 全项目统一分页条（参考示例：共 N 条数据 + 页码 + 上一页/下一页 + 每页条数下拉 + 跳至 X 页）
// 客户端分页用 usePager；后端分页直接用 <Pagination> 接 page/pageSize/total 与 onChange。

const DEFAULT_PAGE_SIZE_OPTIONS = [10, 50, 80, 100];

export interface PaginationProps {
  total: number;
  /** 1 起始页码 */
  page: number;
  pageSize: number;
  /** 翻页与改每页条数统一回调；改条数时由调用方（或 usePager）回到第 1 页 */
  onChange: (page: number, pageSize: number) => void;
  pageSizeOptions?: number[];
  className?: string;
}

export function totalPagesOf(total: number, pageSize: number): number {
  return Math.max(1, Math.ceil(total / Math.max(1, pageSize)));
}

export function safePage(page: number, total: number, pageSize: number): number {
  return Math.min(Math.max(1, page), totalPagesOf(total, pageSize));
}

// 页码序列：首末页固定 + 当前页 ±2，-1 表示省略号
function pageSequence(page: number, totalPages: number): number[] {
  const set = new Set<number>([1, totalPages]);
  for (let p = page - 2; p <= page + 2; p += 1) {
    if (p >= 1 && p <= totalPages) set.add(p);
  }
  const sorted = [...set].sort((a, b) => a - b);
  const seq: number[] = [];
  let prev = 0;
  for (const p of sorted) {
    if (p - prev > 1) seq.push(-1);
    seq.push(p);
    prev = p;
  }
  return seq;
}

export function Pagination({ total, page, pageSize, onChange, pageSizeOptions = DEFAULT_PAGE_SIZE_OPTIONS, className }: PaginationProps) {
  const totalPages = totalPagesOf(total, pageSize);
  const current = safePage(page, total, pageSize);
  const [jump, setJump] = useState("");
  useEffect(() => setJump(""), [current, totalPages]);
  // 空列表不渲染分页条（各列表有自己的空态提示）
  if (total <= 0) return null;
  return (
    <div className={"zt-pager" + (className ? ` ${className}` : "")}>
      <span className="zt-pager-total">共 {total} 条数据</span>
      <div className="zt-pager-pages">
        <button
          type="button"
          className="zt-pager-btn nav"
          disabled={current <= 1}
          aria-label="上一页"
          onClick={() => onChange(current - 1, pageSize)}
        >
          &lt;
        </button>
        {pageSequence(current, totalPages).map((p, i) =>
          p === -1 ? (
            <span key={`ellipsis-${i}`} className="zt-pager-ellipsis">…</span>
          ) : (
            <button
              key={p}
              type="button"
              className={"zt-pager-btn" + (p === current ? " active" : "")}
              onClick={() => onChange(p, pageSize)}
            >
              {p}
            </button>
          ),
        )}
        <button
          type="button"
          className="zt-pager-btn nav"
          disabled={current >= totalPages}
          aria-label="下一页"
          onClick={() => onChange(current + 1, pageSize)}
        >
          &gt;
        </button>
      </div>
      <select
        className="zt-pager-size"
        value={pageSize}
        aria-label="每页条数"
        onChange={(e) => onChange(1, Number(e.target.value))}
      >
        {pageSizeOptions.map((n) => (
          <option key={n} value={n}>{n} 条/页</option>
        ))}
      </select>
      <span className="zt-pager-jump">
        跳至
        <input
          value={jump}
          placeholder={String(current)}
          aria-label="跳至指定页"
          onChange={(e) => setJump(e.target.value.replace(/[^\d]/g, ""))}
          onKeyDown={(e) => {
            if (e.key !== "Enter") return;
            const target = Number(jump);
            if (jump && Number.isFinite(target) && target >= 1) onChange(safePage(target, total, pageSize), pageSize);
            setJump("");
          }}
          onBlur={() => setJump("")}
        />
        页
      </span>
    </div>
  );
}

// 客户端分页 hook：传入全量（或已筛选）数组，返回分页状态与当前页切片。
// 筛选导致数据变少时 pageSafe 自动收敛到合法页码，不需要调用方手动重置页码；
// 每页条数变更自动回到第 1 页。
export function usePager<T>(items: T[], defaultPageSize = 10) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(defaultPageSize);
  const total = items.length;
  const pageSafe = safePage(page, total, pageSize);
  const onChange = (p: number, s: number) => {
    if (s !== pageSize) {
      setPageSize(s);
      setPage(1);
    } else {
      setPage(p);
    }
  };
  const start = (pageSafe - 1) * pageSize;
  return {
    page: pageSafe,
    pageSize,
    total,
    setPage,
    setPageSize,
    onChange,
    paged: items.slice(start, start + pageSize),
  };
}
