import type { ReactNode } from "react";
import { CommandPalette } from "../components/CommandPalette";
import { Sidebar } from "./Sidebar";
import { StatusBar } from "./StatusBar";
import { TopBar } from "./TopBar";

export function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="h-screen flex flex-col">
      <div className="flex-1 flex overflow-hidden">
        <Sidebar />
        <main className="flex-1 flex flex-col overflow-hidden">
          <TopBar />
          <div className="flex-1 overflow-auto p-6">{children}</div>
        </main>
      </div>
      <StatusBar />
      {/* Mounted at the shell so Ctrl/Cmd+K works from every page. Renders
          null until opened, so it costs nothing while closed. */}
      <CommandPalette />
    </div>
  );
}
