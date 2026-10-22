import { useEffect, useState } from "react";
import { CandidatesPage } from "./pages/Candidates";
import { Compare } from "./pages/Compare";
import { DifferentialPage } from "./pages/Differential";
import { ExperimentsPage } from "./pages/Experiments";
import { ExplorePage } from "./pages/Explore";
import { MinimizePage } from "./pages/Minimize";
import { Library } from "./pages/Library";
import { Overview } from "./pages/Overview";
import { Report } from "./pages/Report";
import { RunBenchmark } from "./pages/RunBenchmark";
import { RunDetail } from "./pages/RunDetail";

function useHash() {
  const [hash, setHash] = useState(() => window.location.hash.slice(1) || "/");
  useEffect(() => {
    const on = () => setHash(window.location.hash.slice(1) || "/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

const NAV = [
  { to: "/", label: "Overview" },
  { to: "/run", label: "Run benchmark" },
  { to: "/compare", label: "Compare runs" },
  { to: "/library", label: "Scenario library" },
  { to: "/report", label: "Research report" },
  { to: "/differential", label: "Differential" },
  { to: "/explore", label: "Exploration" },
  { to: "/minimize", label: "Minimization" },
  { to: "/experiments", label: "Experiments" },
  { to: "/candidates", label: "Candidates" },
];

type Theme = "system" | "light" | "dark";

export function App() {
  const hash = useHash();
  const [path, query] = hash.split("?");
  const params = new URLSearchParams(query ?? "");
  const [theme, setTheme] = useState<Theme>(() => {
    try { return (localStorage.getItem("gauntlet-theme") as Theme) || "system"; } catch { return "system"; }
  });
  useEffect(() => {
    if (theme === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("gauntlet-theme", theme); } catch { /* ignore */ }
  }, [theme]);

  let page;
  const runMatch = /^\/runs\/([\w-]+)(?:\/(\w+))?$/.exec(path);
  if (runMatch) {
    const tab = (runMatch[2] as "timeline") || "timeline";
    page = <RunDetail id={runMatch[1]} tab={tab} setTab={(t) => (window.location.hash = `/runs/${runMatch[1]}/${t}`)} />;
  } else if (path === "/run") page = <RunBenchmark />;
  else if (path === "/compare")
    page = <Compare a={params.get("a") ?? ""} b={params.get("b") ?? ""} go={(a, b) => (window.location.hash = `/compare?a=${a}&b=${b}`)} />;
  else if (path === "/library") page = <Library />;
  else if (path.startsWith("/differential"))
    page = <DifferentialPage id={path.split("/")[2] ?? ""} go={(id) => (window.location.hash = `/differential/${id}`)} />;
  else if (path.startsWith("/explore"))
    page = <ExplorePage id={path.split("/")[2] ?? ""} go={(id) => (window.location.hash = `/explore/${id}`)} />;
  else if (path.startsWith("/minimize"))
    page = <MinimizePage id={path.split("/")[2] ?? ""} runParam={params.get("run") ?? ""} go={(id) => (window.location.hash = `/minimize/${id}`)} />;
  else if (path.startsWith("/candidates"))
    page = <CandidatesPage id={path.split("/")[2] ?? ""} go={(id) => (window.location.hash = `/candidates/${id}`)} />;
  else if (path.startsWith("/experiments"))
    page = <ExperimentsPage id={path.split("/")[2] ?? ""} go={(id) => (window.location.hash = `/experiments/${id}`)} />;
  else if (path === "/report") page = <Report />;
  else page = <Overview />;

  const active = (to: string) => (to === "/" ? path === "/" : path.startsWith(to) || (to === "/run" && path.startsWith("/runs")));

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">▚</span>
          <span className="brand-name">Gauntlet</span>
          <span className="brand-sub">adversarial evaluation · research prototype</span>
        </div>
        <nav aria-label="Main">
          {NAV.map((n) => (
            <a key={n.to} href={`#${n.to}`} className={active(n.to) ? "active" : ""} aria-current={active(n.to) ? "page" : undefined}>{n.label}</a>
          ))}
        </nav>
        <select className="theme" value={theme} onChange={(e) => setTheme(e.target.value as Theme)} aria-label="Colour theme">
          <option value="system">System theme</option>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </header>
      <main className="content">{page}</main>
      <footer className="footer muted small">
        Every figure on these pages comes from runs executed by the backend. No external AI agent has been evaluated in this prototype.
      </footer>
    </div>
  );
}
