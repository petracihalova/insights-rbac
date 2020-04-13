#
# Copyright 2019 Red Hat, Inc.
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

"""Model for policy management."""
import logging
from uuid import uuid4

from django.db import connections, models
from django.db.models import signals
from django.utils import timezone
from management.cache import AccessCache
from management.group.model import Group
from management.principal.model import Principal
from management.rbac_fields import AutoDateTimeField
from management.role.model import Role


logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Policy(models.Model):
    """A policy."""

    uuid = models.UUIDField(default=uuid4, editable=False,
                            unique=True, null=False)
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(null=True)
    group = models.ForeignKey(Group, null=True, on_delete=models.CASCADE,
                              related_name='policies')
    roles = models.ManyToManyField(Role, related_name='policies')
    system = models.BooleanField(default=False)
    created = models.DateTimeField(default=timezone.now)
    modified = AutoDateTimeField(default=timezone.now)

    class Meta:
        ordering = ['name', 'modified']


def policy_deleted_cache_handler(sender=None, instance=None, using=None, **kwargs):
    """Signal handler for Principal cache expiry on Policy deletion."""
    logger.info('Handling signal for deleted policy %s - invalidating associated user cache keys', instance)
    cache = AccessCache(connections[using].schema_name)
    if instance.group:
        for principal in instance.group.principals.all():
            cache.delete_policy(principal.uuid)


def policy_to_roles_cache_handler(sender=None, instance=None, action=None,  # noqa: C901
                                  reverse=None, model=None, pk_set=None, using=None,
                                  **kwargs):
    """Signal handler for Principal cache expiry on Policy/Role m2m change."""
    cache = AccessCache(connections[using].schema_name)
    if action in ('post_add', 'pre_remove'):
        logger.info('Handling signal for %s roles change - invalidating policy cache', instance)
        if isinstance(instance, Policy):
            # One or more roles was added to/removed from the policy
            if instance.group:
                for principal in instance.group.principals.all():
                    cache.delete_policy(principal.uuid)
        elif isinstance(instance, Role):
            # One or more policies was added to/removed from the role
            for policy in Policy.objects.filter(pk__in=pk_set):
                if policy.group:
                    for principal in policy.group.principals.all():
                        cache.delete_policy(principal.uuid)
    elif action == 'pre_clear':
        logger.info('Handling signal for %s policy-roles clearing - invalidating policy cache', instance)
        if isinstance(instance, Policy):
            # All roles are being removed from this policy
            if instance.group:
                for principal in instance.group.principals.all():
                    cache.delete_policy(principal.uuid)
        elif isinstance(instance, Role):
            # All policies are being removed from this role
            for principal in Principal.objects.filter(group__policy__role__pk=instance.pk):
                cache.delete_policy(principal.uuid)


signals.pre_delete.connect(policy_deleted_cache_handler, sender=Policy)
signals.m2m_changed.connect(policy_to_roles_cache_handler, sender=Policy.roles.through)
