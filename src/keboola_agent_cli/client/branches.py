"""Development branches and branch metadata.

Extracted verbatim from the former single-file ``client.py`` (issue #520).
"""

from typing import Any

from ..constants import METADATA_NOT_FOUND
from ._core import _CoreClient


class _BranchesMixin(_CoreClient):
    """Development branches and branch metadata."""

    def create_dev_branch(self, name: str, description: str = "") -> dict[str, Any]:
        """Create a new development branch (waits for async job to complete).

        The Storage API returns an async job. This method polls until the job
        completes and returns the branch data from the job results.

        Args:
            name: Branch name.
            description: Optional branch description.

        Returns:
            Branch dict with id, name, description, created, etc.

        Raises:
            KeboolaApiError: If the API call or job fails.
        """
        body: dict[str, str] = {"name": name}
        if description:
            body["description"] = description
        response = self._request("POST", "/v2/storage/dev-branches", json=body)
        job = self._wait_for_storage_job(response.json())
        return job.get("results", {})

    def delete_dev_branch(self, branch_id: int) -> None:
        """Delete a development branch (waits for async job to complete).

        Args:
            branch_id: The branch ID to delete.

        Raises:
            KeboolaApiError: If the API call or job fails.
        """
        response = self._request("DELETE", f"/v2/storage/dev-branches/{branch_id}")
        self._wait_for_storage_job(response.json())

    def list_dev_branches(self) -> list[dict[str, Any]]:
        """List development branches for the project.

        Returns:
            List of branch dicts from the API.
        """
        response = self._request("GET", "/v2/storage/dev-branches")
        return response.json()

    def list_branch_metadata(self, branch_id: int | str = "default") -> list[dict[str, Any]]:
        """List metadata entries on a branch.

        GET /v2/storage/branch/{id}/metadata

        Args:
            branch_id: Branch ID or the literal "default" for the main branch.

        Returns:
            List of metadata dicts with keys: id, key, value, provider, timestamp.
        """
        response = self._request("GET", f"/v2/storage/branch/{branch_id}/metadata")
        return response.json()

    def set_branch_metadata(
        self,
        entries: list[tuple[str, str]],
        branch_id: int | str = "default",
    ) -> list[dict[str, Any]]:
        """Bulk-set metadata key/value pairs on a branch.

        POST /v2/storage/branch/{id}/metadata

        Keboola's endpoint expects PHP-style array indices in the
        form-urlencoded body, e.g.::

            metadata[0][key]=KBC.projectDescription
            metadata[0][value]=My project

        httpx's ``data=`` accepts a mapping of str -> str and URL-encodes it.
        Since each ``metadata[i][...]`` key is unique per index, a plain dict
        preserves both ordering (Python 3.7+) and Keboola's expected shape.

        Args:
            entries: Ordered list of ``(key, value)`` metadata tuples.
            branch_id: Branch ID or the literal "default" for the main branch.

        Returns:
            List of metadata dicts created/updated by the API.
        """
        form: dict[str, str] = {}
        for i, (key, value) in enumerate(entries):
            form[f"metadata[{i}][key]"] = key
            form[f"metadata[{i}][value]"] = value
        response = self._request(
            "POST",
            f"/v2/storage/branch/{branch_id}/metadata",
            data=form,
        )
        return response.json()

    def delete_branch_metadata(
        self,
        metadata_id: int | str,
        branch_id: int | str = "default",
    ) -> None:
        """Delete a single metadata entry on a branch by its numeric ID.

        DELETE /v2/storage/branch/{id}/metadata/{metadataId}

        Args:
            metadata_id: ID of the metadata entry (from ``list_branch_metadata``).
            branch_id: Branch ID or the literal "default" for the main branch.
        """
        self._request(
            "DELETE",
            f"/v2/storage/branch/{branch_id}/metadata/{metadata_id}",
        )

    def get_branch_metadata_value(
        self,
        key: str,
        branch_id: int | str = "default",
    ) -> str | None | object:
        """Return the value for a single metadata key on a branch, or None if absent.

        Convenience wrapper around ``list_branch_metadata`` that filters by key.

        Args:
            key: Metadata key to look up (e.g. "KBC.projectDescription").
            branch_id: Branch ID or the literal "default" for the main branch.

        Returns:
            The string value if the key exists (may be None if the API stored null),
            or ``METADATA_NOT_FOUND`` sentinel if the key is not present.
        """
        for entry in self.list_branch_metadata(branch_id=branch_id):
            if entry.get("key") == key:
                return entry.get("value")
        return METADATA_NOT_FOUND
