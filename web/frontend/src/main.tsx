import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    // networkMode "always": every request goes to the same localhost origin
    // that served the SPA, so react-query's online/offline heuristic is
    // never right here -- with the default "online" mode a browser that
    // (correctly or spuriously) reports offline silently *pauses* queries
    // and mutations instead of failing them, and the UI freezes with no
    // spinner and no error while `kbagent serve` keeps working fine.
    queries: {
      networkMode: "always",
      retry: 1,
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
    mutations: {
      networkMode: "always",
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
