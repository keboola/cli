import { PageTitle } from "../components/Empty";

/**
 * Cross-project jobs feed: one `GET /jobs` call with no `project` param, the
 * server fans out over every registered project in parallel and returns the
 * merged `{jobs, errors}` envelope with `project_alias` stamped on each row.
 */
export function JobsAllPage() {
  return <PageTitle title="All Jobs" description="Jobs across all projects" />;
}
