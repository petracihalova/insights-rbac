#
# Copyright 2024 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

"""Class to handle Dual Write API related operations."""
import logging
from typing import Optional

from django.conf import settings
from kessel.relations.v1beta1 import common_pb2
from management.models import Workspace
from management.relation_replicator.noop_replicator import NoopReplicator
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import DualWriteException
from management.relation_replicator.relation_replicator import RelationReplicator
from management.relation_replicator.relation_replicator import ReplicationEvent
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.role.model import BindingMapping, Role
from migration_tool.migrate import migrate_role
from migration_tool.sharedSystemRolesReplicatedRoleBindings import v1_perm_to_v2_perm
from migration_tool.utils import create_relationship


from api.models import Tenant


logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class RelationApiDualWriteHandler:
    """Class to handle Dual Write API related operations."""

    _replicator: RelationReplicator

    @classmethod
    def for_system_role_event(
        cls,
        role: Role,
        # TODO: may want to include Policy instead?
        tenant: Tenant,
        event_type: ReplicationEventType,
        replicator: Optional[RelationReplicator] = None,
    ):
        """Create a RelationApiDualWriteHandler for assigning / unassigning a system role for a group."""
        return cls(role, event_type, replicator, tenant)

    def __init__(
        self,
        role: Role,
        event_type: ReplicationEventType,
        replicator: Optional[RelationReplicator] = None,
        tenant: Optional[Tenant] = None,
    ):
        """Initialize RelationApiDualWriteHandler."""
        if not self.replication_enabled():
            self._replicator = NoopReplicator()
            return
        try:
            self._replicator = replicator if replicator else OutboxReplicator()
            self.event_type = event_type
            self.role_relations: list[common_pb2.Relationship] = []
            self.current_role_relations: list[common_pb2.Relationship] = []
            self.role = role
            self.binding_mappings: dict[str, BindingMapping] = {}

            binding_tenant = tenant if tenant is not None else role.tenant

            if binding_tenant.tenant_name == "public":
                raise DualWriteException(
                    "Cannot bind role to public tenant. "
                    "Expected the role to have non-public tenant, or for a non-public tenant to be provided. "
                    f"Role: {role.uuid} "
                    f"Tenant: {binding_tenant.id}"
                )

            self.tenant_id = binding_tenant.id
            self.default_workspace = Workspace.objects.get(tenant=binding_tenant, type=Workspace.Types.DEFAULT)
        except Exception as e:
            logger.error(f"Failed to initialize RelationApiDualWriteHandler with error: {e}")
            raise DualWriteException(e)

    def replication_enabled(self):
        """Check whether replication enabled."""
        return settings.REPLICATION_TO_RELATION_ENABLED is True

    def prepare_for_update(self):
        """Generate relations from current state of role and UUIDs for v2 role and role binding from database."""
        if not self.replication_enabled():
            return
        try:
            logger.info(
                "[Dual Write] Generate relations from current state of role(%s): '%s'", self.role.uuid, self.role.name
            )

            self.binding_mappings = {m.id: m for m in self.role.binding_mappings.select_for_update().all()}

            if not self.binding_mappings:
                logger.warning(
                    "[Dual Write] Binding mappings not found for role(%s): '%s'. "
                    "Assuming no current relations exist. "
                    "If this is NOT the case, relations are inconsistent!",
                    self.role.uuid,
                    self.role.name,
                )
                return

            relations, _ = migrate_role(
                self.role,
                default_workspace=self.default_workspace,
                current_bindings=self.binding_mappings.values(),
            )

            self.current_role_relations = relations
        except Exception as e:
            raise DualWriteException(e)

    def replicate_new_or_updated_role(self, role):
        """Generate replication event to outbox table."""
        if not self.replication_enabled():
            return
        self.role = role
        self._generate_relations_and_mappings_for_role()
        self._replicate()

    def replicate_deleted_role(self):
        """Replicate removal of current role state."""
        if not self.replication_enabled():
            return
        self._replicate()

    def _replicate(self):
        if not self.replication_enabled():
            return
        try:
            self._replicator.replicate(
                ReplicationEvent(
                    event_type=self.event_type,
                    info={"v1_role_uuid": str(self.role.uuid)},
                    # TODO: need to think about partitioning
                    # Maybe resource id
                    partition_key="rbactodo",
                    remove=self.current_role_relations,
                    add=self.role_relations,
                ),
            )
        except Exception as e:
            raise DualWriteException(e)

    def _generate_relations_and_mappings_for_role(self):
        """Generate relations and mappings for a role with new UUIDs for v2 role and role bindings."""
        if not self.replication_enabled():
            return []
        try:
            logger.info("[Dual Write] Generate new relations from role(%s): '%s'", self.role.uuid, self.role.name)

            relations, mappings = migrate_role(
                self.role,
                default_workspace=self.default_workspace,
                current_bindings=self.binding_mappings.values(),
            )

            prior_mappings = self.binding_mappings

            self.role_relations = relations
            self.binding_mappings = {m.id: m for m in mappings}

            # Create or update mappings as needed
            for mapping in mappings:
                if mapping.id is not None:
                    prior_mappings.pop(mapping.id)
                mapping.save()

            # Delete any mappings to resources this role no longer gives access to
            for mapping in prior_mappings.values():
                mapping.delete()

            return relations
        except Exception as e:
            raise DualWriteException(e)

    # TODO: Remove/replace - placeholder for testing
    def replicate_new_system_role_permissions(self, role: Role):
        """Replicate system role permissions."""
        if not self.replication_enabled():
            return
        permissions = list()
        for access in role.access.all():
            v1_perm = access.permission
            v2_perm = v1_perm_to_v2_perm(v1_perm)
            permissions.append(v2_perm)

        for permission in permissions:
            self.role_relations.append(
                create_relationship(("rbac", "role"), str(role.uuid), ("rbac", "principal"), str("*"), permission)
            )
        self._replicate()