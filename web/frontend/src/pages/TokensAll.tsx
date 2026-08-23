import { PageTitle } from "../components/Empty";

/**
 * Cross-project token audit: one `GET /token/list` call with no `project`
 * param, the server fans out over every registered project in parallel and
 * returns the merged `{tokens, errors}` envelope with `project_alias` stamped
 * on each row.
 */
export function TokensAllPage() {
  return <PageTitle title="All Tokens" description="Tokens across all projects" />;
}
