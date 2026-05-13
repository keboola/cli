/**
 * Light/dark theme context with localStorage persistence.
 *
 * Strategy: tailwind ``darkMode: 'class'`` -- toggling the ``dark`` class on
 * the <html> element flips every ``dark:`` utility variant in one shot.
 * Body styles and component utilities (``nerd-card``, ``nerd-btn``, ...) all
 * carry both light defaults and dark overrides via ``dark:`` prefixes, so
 * adding/removing the class is the *only* effect of theme switching.
 *
 * Persistence: ``localStorage["kbagent.theme"]`` = "light" | "dark". On first
 * load with no saved value we read ``prefers-color-scheme`` so users land on
 * the variant that matches their OS preference; explicit user choice always
 * wins thereafter.
 */

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";

type Theme = "light" | "dark";

interface ThemeCtx {
  theme: Theme;
  toggle: () => void;
  setTheme: (t: Theme) => void;
}

const ThemeContext = createContext<ThemeCtx | null>(null);

const STORAGE_KEY = "kbagent.theme";

function readInitialTheme(): Theme {
  if (typeof window === "undefined") return "dark";
  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  // First-visit fallback: honour the OS-level preference, defaulting to dark
  // (matches the historical kbagent UI default before the toggle existed).
  if (window.matchMedia?.("(prefers-color-scheme: light)").matches) return "light";
  return "dark";
}

function applyThemeToDom(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "dark") {
    root.classList.add("dark");
    root.style.colorScheme = "dark";
  } else {
    root.classList.remove("dark");
    root.style.colorScheme = "light";
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(readInitialTheme);

  // Apply on mount + every change. Done in an effect so the very first paint
  // matches the initial value chosen by ``readInitialTheme`` (no FOUC flash
  // -- the index.html already starts with ``class="dark"`` and the effect
  // strips it on the first render if needed).
  useEffect(() => {
    applyThemeToDom(theme);
    window.localStorage.setItem(STORAGE_KEY, theme);
  }, [theme]);

  const setTheme = useCallback((t: Theme) => setThemeState(t), []);
  const toggle = useCallback(() => setThemeState((t) => (t === "dark" ? "light" : "dark")), []);

  return (
    <ThemeContext.Provider value={{ theme, toggle, setTheme }}>{children}</ThemeContext.Provider>
  );
}

export function useTheme(): ThemeCtx {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside ThemeProvider");
  return ctx;
}
