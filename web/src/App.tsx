import { useEffect, useState } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import { Shell } from "./components/Shell";
import { getJson, type Me } from "./lib/api";
import { NocContext, NocPage, useNocPoll } from "./pages/Noc";
import { AddDevicesPage } from "./pages/AddDevices";
import { AutomationPage } from "./pages/Automation";
import { DependencyMapPage } from "./pages/DependencyMap";
import { ForgeAiPage } from "./pages/ForgeAi";
import { RacksPage } from "./components/Racks";
import { IncidentDetailPage } from "./pages/IncidentDetail";
import { IncidentsPage } from "./pages/Incidents";
import { InventoryPage } from "./pages/Inventory";
import { InvestigationsPage } from "./pages/Investigations";
import { OnCallPage } from "./pages/OnCall";
import { NotFound } from "./pages/NotFound";
import { ReportsPage } from "./pages/Reports";
import { SettingsPage } from "./pages/Settings";
import { TvPage } from "./pages/Tv";

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const location = useLocation();
  useEffect(() => {
    getJson<Me>("/api/v1/me").then(setMe).catch(() => setMe(null));
  }, []);
  // The wall screen has no menu and polls its own endpoint.
  if (location.pathname === "/tv") return <TvPage />;
  return (
    <ShellWithAi me={me}>
      <Routes>
        <Route path="/" element={<NocPage />} />
        <Route path="/incidents" element={<IncidentsPage />} />
        <Route path="/incidents/on-call" element={<OnCallPage />} />
        <Route path="/incidents/:number" element={<IncidentDetailPage />} />
        <Route path="/inventory" element={<InventoryPage />} />
        <Route path="/inventory/map" element={<DependencyMapPage />} />
        <Route path="/inventory/racks" element={<RacksPage />} />
        <Route path="/inventory/add" element={<AddDevicesPage />} />
        <Route path="/automation" element={<AutomationPage />} />
        <Route path="/investigations" element={<InvestigationsPage />} />
        <Route path="/forgeai" element={<ForgeAiPage />} />
        <Route path="/reports" element={<ReportsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<NotFound />} />
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
