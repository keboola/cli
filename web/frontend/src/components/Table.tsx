import type { ChangeEvent, ReactNode } from "react";

export interface Column<T> {
  header: string;
  cell: (row: T) => ReactNode;
  width?: string;
  align?: "left" | "right";
}

export function DataTable<T>({
  rows,
  columns,
  rowKey,
  onRowClick,
  emptyMessage = "No data",
  selectedKeys,
  onToggleRow,
  onToggleAll,
}: {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  emptyMessage?: string;
  /**
   * When provided together with `onToggleRow`, a leading checkbox column is
   * rendered and rows whose key is in this set show as checked. Toggling a
   * checkbox does not trigger `onRowClick`.
   */
  selectedKeys?: Set<string>;
  onToggleRow?: (key: string, checked: boolean) => void;
  onToggleAll?: (checked: boolean) => void;
}) {
  const selectable = !!selectedKeys && !!onToggleRow;

  if (rows.length === 0) {
    return (
      <div className="nerd-card text-zinc-500 text-sm text-center py-6">{emptyMessage}</div>
    );
  }

  const allSelected = selectable && rows.every((r) => selectedKeys!.has(rowKey(r)));
  const someSelected = selectable && !allSelected && rows.some((r) => selectedKeys!.has(rowKey(r)));

  return (
    <div className="nerd-card overflow-auto p-0">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-zinc-200 text-xs text-zinc-500 uppercase tracking-wider dark:border-zinc-800">
            {selectable && (
              <th className="px-3 py-2 text-left font-normal" style={{ width: "2.5rem" }}>
                <input
                  type="checkbox"
                  aria-label="Select all"
                  checked={allSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = someSelected;
                  }}
                  onChange={(e: ChangeEvent<HTMLInputElement>) => onToggleAll?.(e.target.checked)}
                />
              </th>
            )}
            {columns.map((col, i) => (
              <th
                key={i}
                className={`px-3 py-2 text-${col.align ?? "left"} font-normal`}
                style={{ width: col.width }}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const key = rowKey(row);
            return (
              <tr
                key={key}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={`border-b border-zinc-100 dark:border-zinc-900/50 ${
                  onRowClick ? "cursor-pointer hover:bg-zinc-100 dark:hover:bg-zinc-900/40" : ""
                }`}
              >
                {selectable && (
                  <td className="px-3 py-2 text-left">
                    <input
                      type="checkbox"
                      aria-label={`Select ${key}`}
                      checked={selectedKeys!.has(key)}
                      onClick={(e) => e.stopPropagation()}
                      onChange={(e: ChangeEvent<HTMLInputElement>) =>
                        onToggleRow!(key, e.target.checked)
                      }
                    />
                  </td>
                )}
                {columns.map((col, i) => (
                  <td key={i} className={`px-3 py-2 text-${col.align ?? "left"}`}>
                    {col.cell(row)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
