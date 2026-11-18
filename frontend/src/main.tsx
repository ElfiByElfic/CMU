import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { initAuth } from "./auth/oidc";
import "./styles.css";

// nejdřív dokončit případný návrat z Keycloaku, pak teprve vykreslit aplikaci
void initAuth().finally(() => {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
});
