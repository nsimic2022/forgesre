import { useEffect, useState } from "react";
import { Route, Routes } from "react-router-dom";
import { Shell } from "./components/Shell";
import { getJson, type Me } from "./lib/api";
import { NocContext, NocPage, useNocPoll } from "./pages/Noc";
import { AutomationPage } from "./pages/Automation";
import { IncidentDetailPage } from "./pages/IncidentDetail";
import { IncidentsPage } from "./pages/Incidents";
import { InventoryPage } from "./pages/Inventory";
import { InvestigationsPage } from "./pages/Investigations";
import { OnCallPage } from "./pages/OnCall";
import { Pending } from "./pages/Pending";
import { ReportsPage } from "./pages/Reports";

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  useEffect(() => {
    getJson<Me>("/api/v1/me").then(setMe).catch(() => setMe(null));
  }, []);
  return (
    <ShellWithAi me={me}>
      <Routes>
        <Route path="/" element={<NocPage />} />
        <Route path="/incidents" element={<IncidentsPage />} />
        <Route path="/incidents/on-call" element={<OnCallPage />} />
        <Route path="/incidents/:number" element={<IncidentDetailPage />} />
        <Route path="/inventory" element={<InventoryPage />} />
        <Route path="/automation" element={<AutomationPage />} />
        <Route path="/investigations" element={<InvestigationsPage />} />
        <Route path="/reports" element={<ReportsPage />} />
        <Route path="/settings/*" element={<Pending title="Settings" classic="/admin" />} />
        <Route path="*" element={<Pending title="Not found" classic="/" />} />
      </Routes>
    </ShellWithAi>
  );
}

function ShellWithAi({ me, children }: { me: Me | null; children: React.ReactNode }) {
  // One poll of /api/v1/noc for the whole app: the home page and the sidebar's ForgeAI chip.
  const noc = useNocPoll();
  return (
    <NocContext.Provider value={noc}>
      <Shell me={me} forgeai={noc.data?.ai.forgeai ?? "off"}>
        {children}
      </Shell>
    </NocContext.Provider>
  );
}
