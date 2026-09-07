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
    const onStorage = (event: StorageEvent) => {
      if (event.key === "openknowledge-theme") {
        sync();
      }
    };
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    const onMedia = () => {
      try {
        if (localStorage.getItem("openknowledge-theme") === null) {
          sync();
        }
      } catch {
      }
    };
    window.addEventListener("openknowledge-theme-change", sync);
    window.addEventListener("storage", onStorage);
    media?.addEventListener?.("change", onMedia);
    return () => {
      window.removeEventListener("openknowledge-theme-change", sync);
      window.removeEventListener("storage", onStorage);
      media?.removeEventListener?.("change", onMedia);
    };
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
      title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      type="button"
    >
      {theme === "dark" ? <Sun aria-hidden="true" size={16} /> : <Moon aria-hidden="true" size={16} />}
      <span>{theme === "dark" ? "Light theme" : "Dark theme"}</span>
    </button>
  );
}
