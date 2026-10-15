export function Loading() {
  return (
    <div className="loading">
      <span className="spinner" />
      加载中…
    </div>
  );
}

export function LoadingScreen() {
  return (
    <div style={{ height: "100vh", display: "grid", placeItems: "center" }}>
      <div className="loading">
        <span className="spinner" />
        正在加载 Z-Torque 控制台…
      </div>
    </div>
  );
}
