"""Variables service -- high-level abstraction over keboola.variables configs.

Presents variables as a property you assign to any Keboola config, hiding the
fact that Keboola stores them server-side as separate ``keboola.variables``
configurations with rows. Agents and users call:

    set_variables(parent_component, parent_config, {key: value, ...})

and the service handles create-if-missing, row update, parent linking, and
encryption of ``#``-prefixed secrets. No YAML authoring, no linking dance, no
keboola.variables-as-resource bookkeeping.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any

from ..errors import ConfigError, KeboolaApiError
from ._encryption import encrypt_secrets_in_config, find_plaintext_secret_keys
from .base import BaseService

logger = logging.getLogger(__name__)

VARIABLES_COMPONENT_ID = "keboola.variables"


@dataclass(frozen=True)
class _CreatedLinkedVariables:
    """Result of the auto-create path -- a new ``keboola.variables`` config + row.

    Named fields replace a positional tuple (CONTRIBUTING.md: multi-value returns
    use a dataclass, never a bare tuple beyond two values).
    """

    variables_id: str
    values_id: str
    values: dict[str, str]
    plaintext_written: list[str]


@dataclass(frozen=True)
class _UpdatedLinkedVariables:
    """Result of the update path -- an existing config's default values row.

    ``plaintext_written`` holds the secret key-paths left unencrypted by an
    allowed plaintext-on-encrypt-failure fallback (``[]`` when encryption
    succeeded, never the values).
    """

    values_id: str
    values: dict[str, str]
    plaintext_written: list[str]


class VariablesService(BaseService):
    """Assign, read, and detach variables on any Keboola config.

    The backing ``keboola.variables`` configuration is an implementation detail
    that callers shouldn't need to know about. On first set, a sibling config
    named ``{parent_name}-vars`` is created; subsequent sets update the same
    default row. ``clear`` unlinks the parent but does NOT delete the backing
    config (it might be shared across configs).
    """

    def get_variables(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Return the current variable values assigned to ``{component_id}/{config_id}``.

        Returns a flat ``{name: value}`` dict (the ``keboola.variables`` row
        structure is flattened). ``linked=False`` means the parent config has no
        ``variables_id`` set -- no variables to report.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            parent = client.get_config_detail(component_id, config_id, branch_id=branch_id)
            parent_configuration = parent.get("configuration") or {}
            variables_id = parent_configuration.get("variables_id")
            values_id = parent_configuration.get("variables_values_id")

            if not variables_id:
                return {
                    "project_alias": alias,
                    "parent_component_id": component_id,
                    "parent_config_id": config_id,
                    "variables_id": None,
                    "values_id": None,
                    "values": {},
                    "linked": False,
                }

            vars_cfg = client.get_config_detail(
                VARIABLES_COMPONENT_ID, variables_id, branch_id=branch_id
            )
            target_row = self._resolve_values_row(vars_cfg, values_id)
            values_dict: dict[str, str] = {}
            if target_row:
                for item in target_row.get("configuration", {}).get("values", []):
                    values_dict[item["name"]] = item["value"]
                if not values_id:
                    values_id = target_row["id"]

            return {
                "project_alias": alias,
                "parent_component_id": component_id,
                "parent_config_id": config_id,
                "variables_id": variables_id,
                "values_id": values_id,
                "values": values_dict,
                "linked": True,
            }
        finally:
            client.close()

    def set_variables(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        variables: dict[str, str],
        *,
        replace: bool = False,
        variables_id: str | None = None,
        values_id: str | None = None,
        branch_id: int | None = None,
        allow_plaintext_fallback: bool = False,
    ) -> dict[str, Any]:
        """Assign variable values to ``{component_id}/{config_id}``.

        Creates a backing ``keboola.variables`` config + default row if the
        parent has no ``variables_id`` set. Otherwise updates the already-linked
        values row. With ``replace=False`` (default), values are merged with the
        existing set; with ``replace=True``, the values array is overwritten
        with exactly the provided dict.

        ``#``-prefixed keys are encrypted via the Encryption API before reaching
        Storage (fail-closed unless ``allow_plaintext_fallback=True``).

        ``variables_id`` / ``values_id`` override the auto-discovery path and
        can be used to attach the parent to a pre-existing variables config.
        """
        if not variables:
            raise ConfigError(
                "set_variables requires at least one variable. Use --var KEY=VALUE (repeatable)."
            )

        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            parent = client.get_config_detail(component_id, config_id, branch_id=branch_id)
            parent_name = parent.get("name", "")
            parent_configuration = copy.deepcopy(parent.get("configuration") or {})

            # project_id is needed for the Encryption API scope; not present on
            # the config response, so fetch from verify_token.
            project_id = client.verify_token().project_id
            # A valid storage token always has a project_id; None only on
            # master tokens which cannot be used here.
            assert project_id is not None

            linked_vars_id = variables_id or parent_configuration.get("variables_id")
            linked_values_id = values_id or parent_configuration.get("variables_values_id")
            action = "updated"

            if not linked_vars_id:
                created = self._create_linked_variables(
                    client=client,
                    project_id=project_id,
                    parent_name=parent_name,
                    parent_component_id=component_id,
                    parent_config_id=config_id,
                    variables=variables,
                    branch_id=branch_id,
                    allow_plaintext_fallback=allow_plaintext_fallback,
                )
                linked_vars_id = created.variables_id
                linked_values_id = created.values_id
                final_values = created.values
                plaintext_written = created.plaintext_written
                action = "created"
            else:
                updated = self._update_linked_variables(
                    client=client,
                    project_id=project_id,
                    variables_id=linked_vars_id,
                    values_id=linked_values_id,
                    variables=variables,
                    replace=replace,
                    branch_id=branch_id,
                    allow_plaintext_fallback=allow_plaintext_fallback,
                )
                linked_values_id = updated.values_id
                final_values = updated.values
                plaintext_written = updated.plaintext_written

            # Ensure the parent config carries the link. Existing-linked path
            # may no-op; auto-create path always writes.
            parent_variables_id = parent_configuration.get("variables_id")
            parent_values_id = parent_configuration.get("variables_values_id")
            if parent_variables_id != linked_vars_id or parent_values_id != linked_values_id:
                parent_configuration["variables_id"] = linked_vars_id
                parent_configuration["variables_values_id"] = linked_values_id
                client.update_config(
                    component_id=component_id,
                    config_id=config_id,
                    configuration=parent_configuration,
                    change_description="Linked variables via kbagent",
                    branch_id=branch_id,
                )

            encrypted_keys = sorted(k for k in variables if k.startswith("#"))
            return {
                "project_alias": alias,
                "parent_component_id": component_id,
                "parent_config_id": config_id,
                "variables_id": linked_vars_id,
                "values_id": linked_values_id,
                "action": action,
                "values": final_values,
                "encrypted_keys": encrypted_keys,
                # Empty unless an allowed plaintext-on-encrypt-failure fallback
                # left secret key-paths unencrypted in the row that was written.
                "plaintext_written": plaintext_written,
            }
        finally:
            client.close()

    def clear_variables(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Unlink variables from ``{component_id}/{config_id}``.

        Strips ``variables_id`` + ``variables_values_id`` from the parent config
        and PUTs it. Does NOT delete the underlying ``keboola.variables`` config
        -- it may be shared across configs, and deletion is a destructive op
        the user should do explicitly via ``config delete``.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            parent = client.get_config_detail(component_id, config_id, branch_id=branch_id)
            parent_configuration = copy.deepcopy(parent.get("configuration") or {})
            was_vars_id = parent_configuration.pop("variables_id", None)
            was_values_id = parent_configuration.pop("variables_values_id", None)

            if was_vars_id or was_values_id:
                client.update_config(
                    component_id=component_id,
                    config_id=config_id,
                    configuration=parent_configuration,
                    change_description="Unlinked variables via kbagent",
                    branch_id=branch_id,
                )

            return {
                "project_alias": alias,
                "parent_component_id": component_id,
                "parent_config_id": config_id,
                "was_linked": bool(was_vars_id),
                "unlinked_variables_id": was_vars_id,
                "unlinked_values_id": was_values_id,
            }
        finally:
            client.close()

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _create_linked_variables(
        self,
        *,
        client: Any,
        project_id: int,
        parent_name: str,
        parent_component_id: str,
        parent_config_id: str,
        variables: dict[str, str],
        branch_id: int | None,
        allow_plaintext_fallback: bool,
    ) -> _CreatedLinkedVariables:
        """Auto-create path: new variables config + default row, parent not yet linked."""
        var_name = (parent_name or parent_config_id) + "-vars"
        schema = [{"name": k, "type": "string"} for k in variables]

        new_var_cfg = client.create_config(
            component_id=VARIABLES_COMPONENT_ID,
            name=var_name,
            description=(f"Auto-created by kbagent for {parent_component_id}/{parent_config_id}"),
            configuration={"variables": schema},
            branch_id=branch_id,
        )
        variables_id = new_var_cfg["id"]

        row_config = self._build_encrypted_row_configuration(
            client=client,
            project_id=project_id,
            variables=variables,
            allow_plaintext_fallback=allow_plaintext_fallback,
        )
        plaintext_written = find_plaintext_secret_keys(row_config)

        new_row = client.create_config_row(
            component_id=VARIABLES_COMPONENT_ID,
            config_id=variables_id,
            name="default",
            configuration=row_config,
            description="Auto-created default row by kbagent",
            branch_id=branch_id,
        )
        return _CreatedLinkedVariables(
            variables_id=variables_id,
            values_id=new_row["id"],
            values=dict(variables),
            plaintext_written=plaintext_written,
        )

    def _update_linked_variables(
        self,
        *,
        client: Any,
        project_id: int,
        variables_id: str,
        values_id: str | None,
        variables: dict[str, str],
        replace: bool,
        branch_id: int | None,
        allow_plaintext_fallback: bool,
    ) -> _UpdatedLinkedVariables:
        """Update path: parent already linked (or explicit --variables-id). Merge or replace."""
        vars_cfg = client.get_config_detail(
            VARIABLES_COMPONENT_ID, variables_id, branch_id=branch_id
        )
        target_row = self._resolve_values_row(vars_cfg, values_id)

        if target_row is None:
            # Linked variables_id exists but no values row -- create the default.
            row_config = self._build_encrypted_row_configuration(
                client=client,
                project_id=project_id,
                variables=variables,
                allow_plaintext_fallback=allow_plaintext_fallback,
            )
            plaintext_written = find_plaintext_secret_keys(row_config)
            new_row = client.create_config_row(
                component_id=VARIABLES_COMPONENT_ID,
                config_id=variables_id,
                name="default",
                configuration=row_config,
                description="Auto-created default row by kbagent",
                branch_id=branch_id,
            )
            self._extend_schema_if_new_keys(
                client=client,
                vars_cfg=vars_cfg,
                variables=variables,
                branch_id=branch_id,
            )
            return _UpdatedLinkedVariables(
                values_id=new_row["id"],
                values=dict(variables),
                plaintext_written=plaintext_written,
            )

        existing_values = target_row.get("configuration", {}).get("values", [])
        existing_dict = {v["name"]: v["value"] for v in existing_values}

        if replace:
            final_values = dict(variables)
        else:
            final_values = dict(existing_dict)
            final_values.update(variables)

        # Encrypt only the NEW values -- existing #-keys are already KBC::-
        # prefixed and collect_secrets skips already-encrypted entries.
        row_config = self._build_encrypted_row_configuration(
            client=client,
            project_id=project_id,
            variables=final_values,
            allow_plaintext_fallback=allow_plaintext_fallback,
        )
        plaintext_written = find_plaintext_secret_keys(row_config)

        client.update_config_row(
            component_id=VARIABLES_COMPONENT_ID,
            config_id=variables_id,
            row_id=target_row["id"],
            configuration=row_config,
            change_description="Updated via kbagent config variables-set",
            branch_id=branch_id,
        )

        self._extend_schema_if_new_keys(
            client=client,
            vars_cfg=vars_cfg,
            variables=final_values,
            branch_id=branch_id,
        )
        return _UpdatedLinkedVariables(
            values_id=target_row["id"],
            values=final_values,
            plaintext_written=plaintext_written,
        )

    @staticmethod
    def _build_encrypted_row_configuration(
        *,
        client: Any,
        project_id: int,
        variables: dict[str, str],
        allow_plaintext_fallback: bool,
    ) -> dict[str, Any]:
        """Shape a ``{values: [...]}`` row config and encrypt ``#``-prefixed entries.

        ``#``-prefixed names keep the prefix (the Encryption API and the
        transformation runner both key off it). :func:`encrypt_secrets_in_config`
        recognizes the ``{name, value}`` list shape directly, so no pre-flatten
        dance is needed.

        Returns the encrypted row config. Callers surface any plaintext-fallback
        leak by passing the returned config to :func:`find_plaintext_secret_keys`
        (key-paths only, never the values).
        """
        row_config: dict[str, Any] = {
            "values": [{"name": k, "value": v} for k, v in variables.items()],
        }
        encrypt_secrets_in_config(
            client,
            project_id,
            VARIABLES_COMPONENT_ID,
            row_config,
            allow_plaintext_fallback=allow_plaintext_fallback,
        )
        return row_config

    @staticmethod
    def _resolve_values_row(
        vars_cfg: dict[str, Any], values_id: str | None
    ) -> dict[str, Any] | None:
        """Return the row matching ``values_id``, or the first row (default)."""
        rows = vars_cfg.get("rows", [])
        if not rows:
            return None
        if values_id:
            return next((r for r in rows if r["id"] == values_id), None)
        return rows[0]

    def _extend_schema_if_new_keys(
        self,
        *,
        client: Any,
        vars_cfg: dict[str, Any],
        variables: dict[str, str],
        branch_id: int | None,
    ) -> None:
        """Add any brand-new keys to the variables config's schema.

        Keboola renders variables whose names appear in
        ``configuration.variables`` (the schema) differently from stray keys in
        the row ``values`` array. Keeping the schema in sync makes new vars
        visible in the UI without a second round-trip.
        """
        existing_schema = vars_cfg.get("configuration", {}).get("variables", [])
        existing_names = {v["name"] for v in existing_schema}
        new_names = {k.lstrip("#") for k in variables} - existing_names
        if not new_names:
            return

        updated_schema = list(existing_schema) + [
            {"name": n, "type": "string"} for n in sorted(new_names)
        ]
        new_configuration = copy.deepcopy(vars_cfg.get("configuration") or {})
        new_configuration["variables"] = updated_schema

        variables_id = vars_cfg["id"]
        try:
            client.update_config(
                component_id=VARIABLES_COMPONENT_ID,
                config_id=variables_id,
                configuration=new_configuration,
                change_description="Schema extended by kbagent",
                branch_id=branch_id,
            )
        except KeboolaApiError as exc:
            # Schema sync is cosmetic (values still work without it). Log and
            # continue rather than fail the whole operation.
            logger.warning("Schema sync failed for variables config %s: %s", variables_id, exc)
