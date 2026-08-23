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
import { JobsAllPage } from "./pages/JobsAll";
import { LineagePage } from "./pages/Lineage";
import { LocalAiPage } from "./pages/LocalAi";
import { SemanticLayerPage } from "./pages/SemanticLayer";
import { MembersPage } from "./pages/Members";
import { OrgPage } from "./pages/Org";
import { ProjectsPage } from "./pages/Projects";
import { SchedulesPage } from "./pages/Schedules";
import { SearchPage } from "./pages/Search";
import { SharingPage } from "./pages/Sharing";
import { StoragePage } from "./pages/Storage";
import { StreamsPage } from "./pages/Streams";
import { TokensPage } from "./pages/Tokens";
import { TokensAllPage } from "./pages/TokensAll";
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
    case "stream":
      return <StreamsPage />;
    case "jobs":
      return <JobsPage />;
    case "jobs-all":
      return <JobsAllPage />;
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
    case "semantic-layer":
      return <SemanticLayerPage />;
    case "sharing":
      return <SharingPage />;
    case "data-apps":
      return <DataAppsPage />;
    case "localai":
      return <LocalAiPage />;
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
    case "tokens":
      return <TokensPage />;
    case "tokens-all":
      return <TokensAllPage />;
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
