/**
 * Read/write the page-owned `?sel=` part of the URL.
 *
 * A thin wrapper over the UI-state context so a page never has to know that
 * the selection is mirrored into the location hash -- it just keeps its own
 * selection state and pushes the id it wants to be shareable:
 *
 *   const [sel, setSel] = useHashSelection();
 *   onRowClick={(row) => { setSelected(row); setSel(row.id); }}
 *   onClose={() => { setSelected(null); setSel(null); }}
 *
 * The value is opaque to everything outside the page that wrote it. Pages
 * with a compound selection join the parts with `/`
 * (e.g. `tables/in.c-main.orders`); the router URL-encodes the whole string,
 * so the separator survives the round trip.
 *
 * The context clears it on every page / project / branch change, so a page
 * only ever sees a selection that belongs to it.
 */
import { useUIState } from "./state";

export function useHashSelection(): [string | null, (s: string | null) => void] {
  const { sel, setSel } = useUIState();
  return [sel, setSel];
}
