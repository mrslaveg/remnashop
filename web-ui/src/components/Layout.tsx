import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";

export function Layout() {
  const { me, logout } = useAuth();

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">Remnashop</div>
        <nav className="nav">
          <NavLink to="/" end>
            Подписка
          </NavLink>
          <NavLink to="/plans">Тарифы</NavLink>
          <NavLink to="/devices">Устройства</NavLink>
        </nav>
        <div className="userbox">
          <span className="muted">{me?.name || me?.email || me?.username || "…"}</span>
          <button type="button" className="secondary" onClick={logout}>
            Выйти
          </button>
        </div>
      </header>
      <main className="content">
        <Outlet />
      </main>
      <style>{`
        .shell { min-height: 100vh; display: flex; flex-direction: column; }
        .topbar {
          display: flex; gap: 1rem; align-items: center; flex-wrap: wrap;
          padding: 0.9rem 1.25rem; border-bottom: 1px solid var(--border);
          background: rgba(18, 24, 32, 0.85); backdrop-filter: blur(8px);
          position: sticky; top: 0; z-index: 10;
        }
        .brand { font-weight: 700; letter-spacing: 0.02em; }
        .nav { display: flex; gap: 0.75rem; flex: 1; }
        .nav a {
          color: var(--text-muted); padding: 0.35rem 0.55rem; border-radius: 8px;
        }
        .nav a.active, .nav a:hover {
          color: var(--text); background: var(--bg-hover); text-decoration: none;
        }
        .userbox { display: flex; gap: 0.75rem; align-items: center; }
        .content { width: min(920px, 100%); margin: 0 auto; padding: 1.5rem 1rem 3rem; }
      `}</style>
    </div>
  );
}
