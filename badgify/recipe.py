# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import logging

from django.db import DEFAULT_DB_ALIAS, IntegrityError
from django.db.models import signals
from django.db.models.query import QuerySet
from django.utils.functional import cached_property

from . import settings
from .compat import get_user_model
from .models import Badge, Award
from .utils import chunks

logger = logging.getLogger('badgify')


class BaseRecipe(object):
    """
    Base class for badge recipes.
    """

    # Badge.name
    name = None

    # Badge.slug
    slug = None

    # Badge.description
    description = None

    # The database on which to perform read queries
    db_read = DEFAULT_DB_ALIAS

    # How many awards to create at once
    max_awards_per_create = settings.MAX_AWARDS_PER_CREATE

    @property
    def image(self):
        raise NotImplementedError('Image must be implemented')

    @property
    def user_ids(self):
        pass

    @property
    def badge(self):
        obj = None
        try:
            obj = Badge.objects.get(slug=self.slug)
        except Badge.DoesNotExist:
            pass
        return obj

    def create_badge(self):
        """
        Saves the badge in the database.
        Returns a tuple: ``badge`` (the badge object), ``created`` (``True``, if
        badge has been created) and ``failed`` (``True`` if an ``IntegrityError``
        occured).
        """
        badge, created, failed = self.badge, False, False
        if badge:
            logger.debug('✓ Badge %s: already created', badge.slug)
        else:
            try:
                kwargs = {'name': self.name, 'image': self.image}
                optional_fields = ['slug', 'description']
                for field in optional_fields:
                    value = getattr(self, field)
                    if value is not None:
                        kwargs[field] = value
                badge = Badge.objects.create(**kwargs)
                created = True
                logger.debug('✓ Badge %s: created', badge.slug)
            except IntegrityError:
                failed = True
                logger.debug('✘ Badge %s: IntegrityError', self.slug)
        return (badge, created, failed)

    def can_perform_awarding(self):
        """
        Checks if we can perform awarding process (is ``user_ids`` property
        defined? Does Badge object exists? and so on). If we can perform db
        operations safely, returns ``True``. Otherwise, ``False``.
        """
        if not self.user_ids:
            logger.debug(
                '✘ Badge %s: no users to check (empty user_ids property)',
                self.slug)
            return False

        if not self.badge:
            logger.debug(
                '✘ Badge %s: does not exist in the database (run badgify_sync badges)',
                self.slug)
            return False

        return True

    def update_badge_users_count(self):
        """
        Denormalizes ``Badge.users.count()`` into ``Bagdes.users_count`` field.
        """
        logger.debug('→ Badge %s: syncing users count...', self.slug)

        badge = self.badge
        updated = False

        if not badge:
            logger.debug(
                '✘ Badge %s: does not exist in the database (run badgify_sync badges)',
                self.slug)
            return (self.slug, updated)

        old_value, new_value = badge.users_count, badge.users.count()

        if old_value != new_value:
            badge.users_count = new_value
            badge.save()
            updated = True

        if updated:
            logger.debug('✓ Badge %s: updated users count (from %d to %d)',
                self.slug,
                old_value,
                new_value)
        else:
            logger.debug('✓ Badge %s: users count up-to-date (%d)',
                self.slug,
                new_value)

        return (badge, updated)

    def get_already_awarded_user_ids(self):
        """
        Returns already awarded user ids and the count.
        """
        already_awarded_ids = self.badge.users.values_list('id', flat=True)
        already_awarded_ids_count = len(already_awarded_ids)

        logger.debug(
            "→ Badge %s: %d users already awarded (fetched from db '%s')",
            self.slug,
            already_awarded_ids_count,
            self.db_read)

        return already_awarded_ids

    def get_current_user_ids(self):
        """
        Returns current user ids and the count.
        """
        current_ids = self.user_ids.using(self.db_read)
        current_ids_count = len(current_ids)

        logger.debug(
            "→ Badge %s: %d users to check (fetched from db '%s')",
            self.slug,
            current_ids_count,
            self.db_read)

        return current_ids

    def get_unawarded_user_ids(self):
        """
        Returns unawarded user ids (need to be saved) and the count.
        """
        already_awarded_ids = self.get_already_awarded_user_ids()
        current_ids = self.get_current_user_ids()
        unawarded_ids = list(set(current_ids) - set(already_awarded_ids))
        unawarded_ids_count = len(unawarded_ids)

        logger.debug(
            '→ Badge %s: %d users need to be awarded',
            self.slug,
            unawarded_ids_count)

        return (unawarded_ids, unawarded_ids_count)

    def save_award_objects(self):
        """
        Returns a list of ``Award`` objects ready to be saved.
        """
        User = get_user_model()
        unawarded_ids, unawarded_ids_count = self.get_unawarded_user_ids()

        if not unawarded_ids:
            return

        done_ids = 0

        for user_ids in chunks(unawarded_ids, self.max_awards_per_create):
            done_ids += self.max_awards_per_create
            actual_count = done_ids if done_ids <= unawarded_ids_count else unawarded_ids_count
            logger.debug("→ Badge %s: creating awards (%d / %d users) -- (db read: %s)",
                self.slug,
                actual_count,
                unawarded_ids_count,
                self.db_read)
            self._bulk_create_awards([
                Award(user=user, badge=self.badge)
                for user in (User.objects.using(self.db_read)
                                         .filter(id__in=user_ids))])

    def _bulk_create_awards(self):
        """
        Saves award objects.
        """
        count = len(objects)
        try:
            Award.objects.bulk_create(objects, batch_size=self.max_awards_per_create)
            if not settings.SKIP_AWARD_POST_SAVE_SIGNAL:
                for obj in objects:
                    signals.post_save.send(sender=obj.__class__, instance=obj, created=True)
        except IntegrityError:
            logger.error('✘ Badge %s: IntegrityError for %d awards', self.slug, count)

    def create_awards(self):
        """
        Create awards.
        """
        if self.can_perform_awarding():
            return self.save_award_objects()
