import { Link, Route, Routes } from "react-router-dom";
import { CreateRun } from "./pages/CreateRun";
import { Report } from "./pages/Report";
import { RunDetail } from "./pages/RunDetail";
import { RunsList } from "./pages/RunsList";
import { Taxonomy } from "./pages/Taxonomy";
import { LanguageProvider, useLanguage } from "./lib/i18n";
import type { Language } from "./lib/i18n";

function LanguageSwitcher() {
  const { language, setLanguage } = useLanguage();
  const options: { value: Language; label: string }[] = [
    { value: "en", label: "EN" },
    { value: "zh", label: "中文" },
  ];
  return (
    <div className="language-switcher">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className={`language-switcher-option ${language === option.value ? "active" : ""}`}
          onClick={() => setLanguage(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

function AppShell() {
  const { t } = useLanguage();
  return (
    <div className="app">
      <header className="app-header">
        <Link to="/" className="brand">
          {t("app.title")}
        </Link>
        <nav className="app-nav">
          <Link to="/taxonomy">{t("nav.taxonomy")}</Link>
        </nav>
        <LanguageSwitcher />
      </header>
      <main>
        <Routes>
          <Route path="/" element={<RunsList />} />
          <Route path="/new" element={<CreateRun />} />
          <Route path="/runs/:runId" element={<RunDetail />} />
          <Route path="/runs/:runId/report" element={<Report />} />
          <Route path="/taxonomy" element={<Taxonomy />} />
        </Routes>
      </main>
    </div>
  );
}

function App() {
  return (
    <LanguageProvider>
      <AppShell />
    </LanguageProvider>
  );
}

export default App;
