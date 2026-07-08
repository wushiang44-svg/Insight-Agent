import { Link, Route, Routes } from "react-router-dom";
import { CreateRun } from "./pages/CreateRun";
import { Report } from "./pages/Report";
import { RunDetail } from "./pages/RunDetail";
import { RunsList } from "./pages/RunsList";

function App() {
  return (
    <div className="app">
      <header className="app-header">
        <Link to="/" className="brand">
          Reddit Product Feedback Insight Agent
        </Link>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<RunsList />} />
          <Route path="/new" element={<CreateRun />} />
          <Route path="/runs/:runId" element={<RunDetail />} />
          <Route path="/runs/:runId/report" element={<Report />} />
        </Routes>
      </main>
    </div>
  );
}

export default App;
