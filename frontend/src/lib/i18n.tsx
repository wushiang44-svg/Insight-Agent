import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { translate } from "./i18nDictionaries";
import type { Language } from "./i18nDictionaries";

export type { Language } from "./i18nDictionaries";

const STORAGE_KEY = "insight-agent-language";

interface LanguageContextValue {
  language: Language;
  setLanguage: (language: Language) => void;
  t: (key: string, vars?: Record<string, string | number>) => string;
}

const LanguageContext = createContext<LanguageContextValue | null>(null);

function loadInitialLanguage(): Language {
  if (typeof window === "undefined") return "en";
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return stored === "zh" || stored === "en" ? stored : "en";
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(loadInitialLanguage);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, language);
  }, [language]);

  const value = useMemo<LanguageContextValue>(
    () => ({
      language,
      setLanguage: setLanguageState,
      t: (key, vars) => translate(language, key, vars),
    }),
    [language],
  );

  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

export function useLanguage(): LanguageContextValue {
  const context = useContext(LanguageContext);
  if (!context) throw new Error("useLanguage must be used within a LanguageProvider");
  return context;
}
