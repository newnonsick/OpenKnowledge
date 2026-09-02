"use client";

import { useEffect, useLayoutEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";

type Theme = "light" | "dark";

function currentTheme(): Theme {
  if (typeof window === "undefined") {
    return "light";
  }
  try {
    const stored = localStorage.getItem("openknowledge-theme");
    if (stored === "dark" || stored === "light") {
      return stored;
    }
  } catch {
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function ThemeToggle({ menuItem = false, onFocus, tabIndex }: { menuItem?: boolean; onFocus?: () => void; tabIndex?: number } = {}) {
  const [theme, setTheme] = useState<Theme>("light");

  useLayoutEffect(() => {
    const resolved = currentTheme();
    document.documentElement.dataset.theme = resolved;
    setTheme(resolved);
  }, []);

  useEffect(() => {
    const sync = () => setTheme(currentTheme());
    window.addEventListener("openknowledge-theme-change", sync);
    return () => window.removeEventListener("openknowledge-theme-change", sync);
  }, []);

  const apply = (next: Theme) => {
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("openknowledge-theme", next);
    } catch {
    }
    setTheme(next);
    window.dispatchEvent(new Event("openknowledge-theme-change"));
  };
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button
      aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      className="theme-toggle"
      onClick={() => apply(next)}
      onFocus={onFocus}
      role={menuItem ? "menuitem" : undefined}
      tabIndex={tabIndex}
      type="button"
    >
      {theme === "dark" ? <Sun aria-hidden="true" size={15} /> : <Moon aria-hidden="true" size={15} />}
      <span>{theme === "dark" ? "Light theme" : "Dark theme"}</span>
    </button>
  );
}
