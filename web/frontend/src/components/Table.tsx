import type { ReactNode } from "react";

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
}: {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  emptyMessage?: string;
}) {
  if (rows.length === 0) {
    return (
      <div className="nerd-card text-zinc-500 text-sm text-center py-6">{emptyMessage}</div>
    );
  }
  return (
    <div className="nerd-card overflow-auto p-0">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-zinc-200 text-xs text-zinc-500 uppercase tracking-wider dark:border-zinc-800">
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
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={`border-b border-zinc-100 dark:border-zinc-900/50 ${
                onRowClick ? "cursor-pointer hover:bg-zinc-100 dark:hover:bg-zinc-900/40" : ""
              }`}
            >
              {columns.map((col, i) => (
                <td key={i} className={`px-3 py-2 text-${col.align ?? "left"}`}>
                  {col.cell(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
