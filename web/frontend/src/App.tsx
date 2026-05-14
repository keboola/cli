import { Shell } from "./layout/Shell";
import { AgentsPage } from "./pages/Agents";
import { BranchesPage } from "./pages/Branches";
import { DashboardPage } from "./pages/Dashboard";
import { ChangelogPage } from "./pages/Changelog";
import { ComponentsPage } from "./pages/Components";
import { ConfigsPage } from "./pages/Configs";
import { DataAppsPage } from "./pages/DataApps";
import { DoctorPage } from "./pages/Doctor";
import { EncryptPage } from "./pages/Encrypt";
import { FlowsPage } from "./pages/Flows";
import { JobsPage } from "./pages/Jobs";
import { KaiPage } from "./pages/Kai";
import { LineagePage } from "./pages/Lineage";
import { McpPage } from "./pages/Mcp";
import { MembersPage } from "./pages/Members";
import { OrgPage } from "./pages/Org";
import { ProjectsPage } from "./pages/Projects";
import { SchedulesPage } from "./pages/Schedules";
import { SearchPage } from "./pages/Search";
import { SharingPage } from "./pages/Sharing";
import { StoragePage } from "./pages/Storage";
import { WorkspacesPage } from "./pages/Workspaces";
import { UIStateProvider, useUIState } from "./state";
import { ThemeProvider } from "./theme";

function Router() {
  const { page } = useUIState();
  switch (page) {
    case "dashboard":
      return <DashboardPage />;
    case "projects":
      return <ProjectsPage />;
    case "configs":
      return <ConfigsPage />;
    case "components":
      return <ComponentsPage />;
    case "storage":
      return <StoragePage />;
    case "jobs":
      return <JobsPage />;
    case "branches":
      return <BranchesPage />;
    case "workspaces":
      return <WorkspacesPage />;
    case "flows":
      return <FlowsPage />;
    case "schedules":
      return <SchedulesPage />;
    case "lineage":
      return <LineagePage />;
    case "sharing":
      return <SharingPage />;
    case "data-apps":
      return <DataAppsPage />;
    case "mcp":
      return <McpPage />;
    case "kai":
      return <KaiPage />;
    case "agents":
      return <AgentsPage />;
    case "search":
      return <SearchPage />;
    case "encrypt":
      return <EncryptPage />;
    case "org":
      return <OrgPage />;
    case "members":
      return <MembersPage />;
    case "doctor":
      return <DoctorPage />;
    case "changelog":
      return <ChangelogPage />;
    default:
      return <DashboardPage />;
  }
}

export function App() {
  return (
    <ThemeProvider>
      <UIStateProvider>
        <Shell>
          <Router />
        </Shell>
      </UIStateProvider>
    </ThemeProvider>
  );
}
