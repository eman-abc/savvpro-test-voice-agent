import { useEffect, useState } from "react";
import { Link, Route, Routes } from "react-router-dom";
import { api } from "./lib/api";
import type { Config } from "./lib/types";
import CallPage from "./pages/CallPage";
import StaffPage from "./pages/StaffPage";

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);

  useEffect(() => {
    api.config().then(setConfig).catch((e: Error) => setConfigError(e.message));
  }, []);

  return (
    <div className="shell">
      <header className="topbar">
        <Link to="/" className="brand">
          <span className="brand-mark" aria-hidden="true" />
          {config?.clinic.name ?? "Clinic assistant"}
        </Link>
        {config?.demo_mode && <span className="tag">Demo · fake data only</span>}
        <span className="spacer" />
        <Link className="navlink hide-sm" to="/">Call the assistant</Link>
        <Link className="navlink" to="/staff">Staff</Link>
      </header>
      <main className="page">
        {configError && <p className="notice error" role="alert">{configError}</p>}
        <Routes>
          <Route path="/" element={<CallPage config={config} />} />
          <Route path="/staff" element={<StaffPage config={config} />} />
          <Route path="*" element={<p className="empty">That page doesn't exist. <Link to="/">Back to the assistant</Link></p>} />
        </Routes>
      </main>
    </div>
  );
}
