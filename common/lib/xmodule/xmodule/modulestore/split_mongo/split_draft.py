"""
Module for the dual-branch fall-back Draft->Published Versioning ModuleStore
"""

from xmodule.modulestore.split_mongo.split import SplitMongoModuleStore, EXCLUDE_ALL
from xmodule.exceptions import InvalidVersionError
from xmodule.modulestore import ModuleStoreEnum
from xmodule.modulestore.exceptions import InsufficientSpecificationError, ItemNotFoundError
from xmodule.modulestore.draft_and_published import (
    ModuleStoreDraftAndPublished, DIRECT_ONLY_CATEGORIES, UnsupportedRevisionError
)
from opaque_keys.edx.locator import CourseLocator, LibraryLocator, LibraryUsageLocator
from xmodule.modulestore.split_mongo import BlockKey
from contracts import contract


class DraftVersioningModuleStore(SplitMongoModuleStore, ModuleStoreDraftAndPublished):
    """
    A subclass of Split that supports a dual-branch fall-back versioning framework
        with a Draft branch that falls back to a Published branch.
    """
    def create_course(self, org, course, run, user_id, skip_auto_publish=False, **kwargs):
        """
        Creates and returns the course.

        Args:
            org (str): the organization that owns the course
            course (str): the name of the course
            run (str): the name of the run
            user_id: id of the user creating the course
            kwargs: Any optional arguments understood by a subset of modulestores to customize instantiation

        Returns: a CourseDescriptor
        """
        master_branch = kwargs.pop('master_branch', ModuleStoreEnum.BranchName.draft)
        with self.bulk_operations(CourseLocator(org, course, run)):
            item = super(DraftVersioningModuleStore, self).create_course(
                org, course, run, user_id, master_branch=master_branch, **kwargs
            )
            if master_branch == ModuleStoreEnum.BranchName.draft and not skip_auto_publish:
                # any other value is hopefully only cloning or doing something which doesn't want this value add
                self._auto_publish_no_children(item.location, item.location.category, user_id, **kwargs)

                # create any other necessary things as a side effect: ensure they populate the draft branch
                # and rely on auto publish to populate the published branch: split's create course doesn't
                # call super b/c it needs the auto publish above to have happened before any of the create_items
                # in this; so, this manually calls the grandparent and above methods.
                with self.branch_setting(ModuleStoreEnum.Branch.draft_preferred, item.id):
                    # NOTE: DO NOT CHANGE THE SUPER. See comment above
                    super(SplitMongoModuleStore, self).create_course(
                        org, course, run, user_id, runtime=item.runtime, **kwargs
                    )

            return item

    def get_course(self, course_id, depth=0, **kwargs):
        course_id = self._map_revision_to_branch(course_id)
        return super(DraftVersioningModuleStore, self).get_course(course_id, depth=depth, **kwargs)

    def get_library(self, library_id, depth=0, head_validation=True, **kwargs):
        if not head_validation and library_id.version_guid:
            return SplitMongoModuleStore.get_library(
                self, library_id, depth=depth, head_validation=head_validation, **kwargs
            )
        library_id = self._map_revision_to_branch(library_id)
        return super(DraftVersioningModuleStore, self).get_library(library_id, depth=depth, **kwargs)

    def clone_course(self, source_course_id, dest_course_id, user_id, fields=None, revision=None, **kwargs):
        """
        See :py:meth: xmodule.modulestore.split_mongo.split.SplitMongoModuleStore.clone_course
        """
        dest_course_id = self._map_revision_to_branch(dest_course_id, revision=revision)
        return super(DraftVersioningModuleStore, self).clone_course(
            source_course_id, dest_course_id, user_id, fields=fields, **kwargs
        )

    def get_courses(self, **kwargs):
        """
        Returns all the courses on the Draft or Published branch depending on the branch setting.
        """
        branch_setting = self.get_branch_setting()
        if branch_setting == ModuleStoreEnum.Branch.draft_preferred:
            return super(DraftVersioningModuleStore, self).get_courses(ModuleStoreEnum.BranchName.draft, **kwargs)
        elif branch_setting == ModuleStoreEnum.Branch.published_only:
            return super(DraftVersioningModuleStore, self).get_courses(ModuleStoreEnum.BranchName.published, **kwargs)
        else:
            raise InsufficientSpecificationError()

    def _auto_publish_no_children(self, location, category, user_id, **kwargs):
        """
        Publishes item if the category is DIRECT_ONLY. This assumes another method has checked that
        location points to the head of the branch and ignores the version. If you call this in any
        other context, you may blow away another user's changes.
        NOTE: only publishes the item at location: no children get published.
        """
        if location.branch == ModuleStoreEnum.BranchName.draft and category in DIRECT_ONLY_CATEGORIES:
            # version_agnostic b/c of above assumption in docstring
            self.publish(location.version_agnostic(), user_id, blacklist=EXCLUDE_ALL, **kwargs)

    def copy_from_template(self, source_keys, dest_key, user_id, **kwargs):
        """
        See :py:meth `SplitMongoModuleStore.copy_from_template`
        """
        source_keys = [self._map_revision_to_branch(key) for key in source_keys]
        dest_key = self._map_revision_to_branch(dest_key)
        head_validation = kwargs.get('head_validation')
        new_keys = super(DraftVersioningModuleStore, self).copy_from_template(
            source_keys, dest_key, user_id, head_validation
        )
        if dest_key.branch == ModuleStoreEnum.BranchName.draft:
            # Check if any of new_keys or their descendants need to be auto-published.
            # We don't use _auto_publish_no_children since children may need to be published.
            with self.bulk_operations(dest_key.course_key):
                keys_to_check = list(new_keys)
                while keys_to_check:
                    usage_key = keys_to_check.pop()
                    if usage_key.category in DIRECT_ONLY_CATEGORIES:
                        self.publish(usage_key.version_agnostic(), user_id, blacklist=EXCLUDE_ALL, **kwargs)
                        children = getattr(self.get_item(usage_key, **kwargs), "children", [])
                        # e.g. if usage_key is a chapter, it may have an auto-publish sequential child
                        keys_to_check.extend(children)
        return new_keys

    def update_item(self, descriptor, user_id, allow_not_found=False, force=False, **kwargs):
        old_descriptor_locn = descriptor.location
        descriptor.location = self._map_revision_to_branch(old_descriptor_locn)
        emit_signals = descriptor.location.branch == ModuleStoreEnum.BranchName.published \
            or descriptor.location.block_type in DIRECT_ONLY_CATEGORIES

        with self.bulk_operations(descriptor.location.course_key, emit_signals=emit_signals):
            item = super(DraftVersioningModuleStore, self).update_item(
                descriptor,
                user_id,
                allow_not_found=allow_not_found,
                force=force,
                **kwargs
            )
            self._auto_publish_no_children(item.location, item.location.category, user_id, **kwargs)
            descriptor.location = old_descriptor_locn
            return item

    def create_item(
        self, user_id, course_key, block_type, block_id=None,
        definition_locator=None, fields=None,
        force=False, skip_auto_publish=False, **kwargs
    ):
        """
        See :py:meth `ModuleStoreDraftAndPublished.create_item`
        """
        course_key = self._map_revision_to_branch(course_key)
        emit_signals = course_key.branch == ModuleStoreEnum.BranchName.published \
            or block_type in DIRECT_ONLY_CATEGORIES
        with self.bulk_operations(course_key, emit_signals=emit_signals):
            item = super(DraftVersioningModuleStore, self).create_item(
                user_id, course_key, block_type, block_id=block_id,
                definition_locator=definition_locator, fields=fields,
                force=force, **kwargs
            )
            if not skip_auto_publish:
                self._auto_publish_no_children(item.location, item.location.category, user_id, **kwargs)
            return item

    def create_child(
            self, user_id, parent_usage_key, block_type, block_id=None,
            fields=None, **kwargs
    ):
        parent_usage_key = self._map_revision_to_branch(parent_usage_key)
        with self.bulk_operations(parent_usage_key.course_key):
            item = super(DraftVersioningModuleStore, self).create_child(
                user_id, parent_usage_key, block_type, block_id=block_id,
                fields=fields, **kwargs
            )
            # Publish both the child and the parent, if the child is a direct-only category
            self._auto_publish_no_children(item.location, item.location.category, user_id, **kwargs)
            self._auto_publish_no_children(parent_usage_key, item.location.category, user_id, **kwargs)
            return item

    def delete_item(self, location, user_id, revision=None, **kwargs):
        """
        Delete the given item from persistence. kwargs allow modulestore specific parameters.

        Args:
            location: UsageKey of the item to be deleted
            user_id: id of the user deleting the item
            revision:
                None - deletes the item and its subtree, and updates the parents per description above
                ModuleStoreEnum.RevisionOption.published_only - removes only Published versions
                ModuleStoreEnum.RevisionOption.all - removes both Draft and Published parents
                    currently only provided by contentstore.views.item.orphan_handler
                Otherwise, raises a ValueError.
        """
        with self.bulk_operations(location.course_key):
            if isinstance(location, LibraryUsageLocator):
                branches_to_delete = [ModuleStoreEnum.BranchName.library]  # Libraries don't yet have draft/publish support
            elif revision == ModuleStoreEnum.RevisionOption.published_only:
                branches_to_delete = [ModuleStoreEnum.BranchName.published]
            elif revision == ModuleStoreEnum.RevisionOption.all:
                branches_to_delete = [ModuleStoreEnum.BranchName.published, ModuleStoreEnum.BranchName.draft]
            elif revision is None:
                branches_to_delete = [ModuleStoreEnum.BranchName.draft]
            else:
                raise UnsupportedRevisionError(
                    [
                        None,
                        ModuleStoreEnum.RevisionOption.published_only,
                        ModuleStoreEnum.RevisionOption.all
                    ]
                )

            self._flag_publish_event(location.course_key)
            for branch in branches_to_delete:
                branched_location = location.for_branch(branch)
                parent_loc = self.get_parent_location(branched_location)
                SplitMongoModuleStore.delete_item(self, branched_location, user_id)
                # publish parent w/o child if deleted element is direct only (not based on type of parent)
                if branch == ModuleStoreEnum.BranchName.draft and branched_location.block_type in DIRECT_ONLY_CATEGORIES:
                    self.publish(parent_loc.version_agnostic(), user_id, blacklist=EXCLUDE_ALL, **kwargs)

    def _map_revision_to_branch(self, key, revision=None):
        """
        Maps RevisionOptions to BranchNames, inserting them into the key
        """
        if isinstance(key, (LibraryLocator, LibraryUsageLocator)):
            # Libraries don't yet have draft/publish support:
            draft_branch = ModuleStoreEnum.BranchName.library
            published_branch = ModuleStoreEnum.BranchName.library
        else:
            draft_branch = ModuleStoreEnum.BranchName.draft
            published_branch = ModuleStoreEnum.BranchName.published

        if revision == ModuleStoreEnum.RevisionOption.published_only:
            return key.for_branch(published_branch)
        elif revision == ModuleStoreEnum.RevisionOption.draft_only:
            return key.for_branch(draft_branch)
        elif revision is None:
            if key.branch is not None:
                return key
            elif self.get_branch_setting(key) == ModuleStoreEnum.Branch.draft_preferred:
                return key.for_branch(draft_branch)
            else:
                return key.for_branch(published_branch)
        else:
            raise UnsupportedRevisionError()

    def has_item(self, usage_key, revision=None):
        """
        Returns True if location exists in this ModuleStore.
        """
        usage_key = self._map_revision_to_branch(usage_key, revision=revision)
        return super(DraftVersioningModuleStore, self).has_item(usage_key)

    def get_item(self, usage_key, depth=0, revision=None, **kwargs):
        """
        Returns the item identified by usage_key and revision.
        """
        usage_key = self._map_revision_to_branch(usage_key, revision=revision)
        return super(DraftVersioningModuleStore, self).get_item(usage_key, depth=depth, **kwargs)

    def get_items(self, course_locator, revision=None, **kwargs):
        """
        Returns a list of XModuleDescriptor instances for the matching items within the course with
        the given course_locator.
        """
        course_locator = self._map_revision_to_branch(course_locator, revision=revision)
        return super(DraftVersioningModuleStore, self).get_items(course_locator, **kwargs)

    def get_parent_location(self, location, revision=None, **kwargs):
        '''
        Returns the given location's parent location in this course.
        Args:
            revision:
                None - uses the branch setting for the revision
                ModuleStoreEnum.RevisionOption.published_only
                    - return only the PUBLISHED parent if it exists, else returns None
                ModuleStoreEnum.RevisionOption.draft_preferred
                    - return either the DRAFT or PUBLISHED parent, preferring DRAFT, if parent(s) exists,
                        else returns None
        '''
        if revision == ModuleStoreEnum.RevisionOption.draft_preferred:
            revision = ModuleStoreEnum.RevisionOption.draft_only
        location = self._map_revision_to_branch(location, revision=revision)
        return super(DraftVersioningModuleStore, self).get_parent_location(location, **kwargs)

    def get_block_original_usage(self, usage_key):
        """
        If a block was inherited into another structure using copy_from_template,
        this will return the original block usage locator from which the
        copy was inherited.
        """
        usage_key = self._map_revision_to_branch(usage_key)
        return super(DraftVersioningModuleStore, self).get_block_original_usage(usage_key)

    def get_orphans(self, course_key, **kwargs):
        course_key = self._map_revision_to_branch(course_key)
        return super(DraftVersioningModuleStore, self).get_orphans(course_key, **kwargs)

    def fix_not_found(self, course_key, user_id):
        """
        Fix any children which point to non-existent blocks in the course's published and draft branches
        """
        for branch in [ModuleStoreEnum.RevisionOption.published_only, ModuleStoreEnum.RevisionOption.draft_only]:
            super(DraftVersioningModuleStore, self).fix_not_found(
                self._map_revision_to_branch(course_key, branch),
                user_id
            )

    def has_changes(self, xblock):
        """
        Checks if the given block has unpublished changes
        :param xblock: the block to check
        :return: True if the draft and published versions differ
        """
        def get_course(branch_name):
            return self._lookup_course(xblock.location.course_key.for_branch(branch_name)).structure

        def get_block(course_structure, block_key):
            return self._get_block_from_structure(course_structure, block_key)

        draft_course = get_course(ModuleStoreEnum.BranchName.draft)
        published_course = get_course(ModuleStoreEnum.BranchName.published)

        def has_changes_subtree(block_key):
            draft_block = get_block(draft_course, block_key)
            if draft_block is None:  # temporary fix for bad pointers TNL-1141
                return True
            published_block = get_block(published_course, block_key)
            if published_block is None:
                return True

            # check if the draft has changed since the published was created
            if self._get_version(draft_block) != self._get_version(published_block):
                return True

            # check the children in the draft
            if 'children' in draft_block.fields:
                return any(
                    [has_changes_subtree(child_block_id) for child_block_id in draft_block.fields['children']]
                )

            return False

        return has_changes_subtree(BlockKey.from_usage_key(xblock.location))

    def publish(self, location, user_id, blacklist=None, **kwargs):
        """
        Publishes the subtree under location from the draft branch to the published branch
        Returns the newly published item.
        """
        super(DraftVersioningModuleStore, self).copy(
            user_id,
            # Directly using the replace function rather than the for_branch function
            # because for_branch obliterates the version_guid and will lead to missed version conflicts.
            # TODO Instead, the for_branch implementation should be fixed in the Opaque Keys library.
            location.course_key.replace(branch=ModuleStoreEnum.BranchName.draft),
            # We clear out the version_guid here because the location here is from the draft branch, and that
            # won't have the same version guid
            location.course_key.replace(branch=ModuleStoreEnum.BranchName.published, version_guid=None),
            [location],
            blacklist=blacklist
        )

        self._flag_publish_event(location.course_key)

        return self.get_item(location.for_branch(ModuleStoreEnum.BranchName.published), **kwargs)

    def unpublish(self, location, user_id, **kwargs):
        """
        Deletes the published version of the item.
        Returns the newly unpublished item.
        """
        with self.bulk_operations(location.course_key):
            self.delete_item(location, user_id, revision=ModuleStoreEnum.RevisionOption.published_only)
            return self.get_item(location.for_branch(ModuleStoreEnum.BranchName.draft), **kwargs)

    def revert_to_published(self, location, user_id):
        """
        Reverts an item to its last published version (recursively traversing all of its descendants).
        If no published version exists, a VersionConflictError is thrown.

        If a published version exists but there is no draft version of this item or any of its descendants, this
        method is a no-op.

        :raises InvalidVersionError: if no published version exists for the location specified
        """
        if location.category in DIRECT_ONLY_CATEGORIES:
            return

        draft_course_key = location.course_key.for_branch(ModuleStoreEnum.BranchName.draft)
        with self.bulk_operations(draft_course_key):

            # get head version of Published branch
            published_course_structure = self._lookup_course(
                location.course_key.for_branch(ModuleStoreEnum.BranchName.published)
            ).structure
            published_block = self._get_block_from_structure(
                published_course_structure,
                BlockKey.from_usage_key(location)
            )
            if published_block is None:
                raise InvalidVersionError(location)

            # create a new versioned draft structure
            draft_course_structure = self._lookup_course(draft_course_key).structure
            new_structure = self.version_structure(draft_course_key, draft_course_structure, user_id)

            # remove the block and its descendants from the new structure
            self._remove_subtree(BlockKey.from_usage_key(location), new_structure['blocks'])

            # copy over the block and its descendants from the published branch
            def copy_from_published(root_block_id):
                """
                copies root_block_id and its descendants from published_course_structure to new_structure
                """
                self._update_block_in_structure(
                    new_structure,
                    root_block_id,
                    self._get_block_from_structure(published_course_structure, root_block_id)
                )
                block = self._get_block_from_structure(new_structure, root_block_id)
                for child_block_id in block.fields.get('children', []):
                    copy_from_published(child_block_id)

            copy_from_published(BlockKey.from_usage_key(location))

            # update course structure and index
            self.update_structure(draft_course_key, new_structure)
            index_entry = self._get_index_if_valid(draft_course_key)
            if index_entry is not None:
                self._update_head(draft_course_key, index_entry, ModuleStoreEnum.BranchName.draft, new_structure['_id'])

    def get_course_history_info(self, course_locator):
        """
        See :py:meth `xmodule.modulestore.split_mongo.split.SplitMongoModuleStore.get_course_history_info`
        """
        course_locator = self._map_revision_to_branch(course_locator)
        return super(DraftVersioningModuleStore, self).get_course_history_info(course_locator)

    def get_course_successors(self, course_locator, version_history_depth=1):
        """
        See :py:meth `xmodule.modulestore.split_mongo.split.SplitMongoModuleStore.get_course_successors`
        """
        course_locator = self._map_revision_to_branch(course_locator)
        return super(DraftVersioningModuleStore, self).get_course_successors(
            course_locator, version_history_depth=version_history_depth
        )

    def get_block_generations(self, block_locator):
        """
        See :py:meth `xmodule.modulestore.split_mongo.split.SplitMongoModuleStore.get_block_generations`
        """
        block_locator = self._map_revision_to_branch(block_locator)
        return super(DraftVersioningModuleStore, self).get_block_generations(block_locator)

    def has_published_version(self, xblock):
        """
        Returns whether this xblock has a published version (whether it's up to date or not).
        """
        return self._get_head(xblock, ModuleStoreEnum.BranchName.published) is not None

    def convert_to_draft(self, location, user_id):
        """
        Create a copy of the source and mark its revision as draft.

        :param source: the location of the source (its revision must be None)
        """
        # This is a no-op in Split since a draft version of the data always remains
        pass

    def _get_head(self, xblock, branch):
        """ Gets block at the head of specified branch """
        try:
            course_structure = self._lookup_course(xblock.location.course_key.for_branch(branch)).structure
        except ItemNotFoundError:
            # There is no published version xblock container, e.g. Library
            return None
        return self._get_block_from_structure(course_structure, BlockKey.from_usage_key(xblock.location))

    def _get_version(self, block):
        """
        Return the version of the given database representation of a block.
        """
        source_version = block.edit_info.source_version
        return source_version if source_version is not None else block.edit_info.update_version

    def import_xblock(self, user_id, course_key, block_type, block_id, fields=None, runtime=None, **kwargs):
        """
        Split-based modulestores need to import published blocks to both branches
        """
        with self.bulk_operations(course_key):
            # hardcode course root block id
            if block_type == 'course':
                block_id = self.DEFAULT_ROOT_COURSE_BLOCK_ID
            elif block_type == 'library':
                block_id = self.DEFAULT_ROOT_LIBRARY_BLOCK_ID
            new_usage_key = course_key.make_usage_key(block_type, block_id)

            # Only the course import process calls import_xblock(). If the branch setting is published_only,
            # then the non-draft blocks are being imported.
            if self.get_branch_setting() == ModuleStoreEnum.Branch.published_only:
                # Override any existing drafts (PLAT-297, PLAT-299). This import/publish step removes
                # any local changes during the course import.
                draft_course = course_key.for_branch(ModuleStoreEnum.BranchName.draft)
                with self.branch_setting(ModuleStoreEnum.Branch.draft_preferred, draft_course):
                    # Importing the block and publishing the block links the draft & published blocks' version history.
                    draft_block = self.import_xblock(user_id, draft_course, block_type, block_id, fields, runtime)
                    return self.publish(draft_block.location.version_agnostic(), user_id, blacklist=EXCLUDE_ALL, **kwargs)

            # do the import
            partitioned_fields = self.partition_fields_by_scope(block_type, fields)
            course_key = self._map_revision_to_branch(course_key)  # cast to branch_setting
            return self._update_item_from_fields(
                user_id, course_key, BlockKey(block_type, block_id), partitioned_fields, None, allow_not_found=True, force=True
            ) or self.get_item(new_usage_key)

    def compute_published_info_internal(self, xblock):
        """
        Get the published branch and find when it was published if it was. Cache the results in the xblock
        """
        published_block = self._get_head(xblock, ModuleStoreEnum.BranchName.published)
        if published_block is not None:
            setattr(xblock, '_published_by', published_block.edit_info.edited_by)
            setattr(xblock, '_published_on', published_block.edit_info.edited_on)

    @contract(asset_key='AssetKey')
    def find_asset_metadata(self, asset_key, **kwargs):
        return super(DraftVersioningModuleStore, self).find_asset_metadata(
            self._map_revision_to_branch(asset_key), **kwargs
        )

    def get_all_asset_metadata(self, course_key, asset_type, start=0, maxresults=-1, sort=None, **kwargs):
        return super(DraftVersioningModuleStore, self).get_all_asset_metadata(
            self._map_revision_to_branch(course_key), asset_type, start, maxresults, sort, **kwargs
        )

    def _update_course_assets(self, user_id, asset_key, update_function):
        """
        Updates both the published and draft branches
        """
        # if one call gets an exception, don't do the other call but pass on the exception
        super(DraftVersioningModuleStore, self)._update_course_assets(
            user_id, self._map_revision_to_branch(asset_key, ModuleStoreEnum.RevisionOption.published_only),
            update_function
        )
        super(DraftVersioningModuleStore, self)._update_course_assets(
            user_id, self._map_revision_to_branch(asset_key, ModuleStoreEnum.RevisionOption.draft_only),
            update_function
        )

    def save_asset_metadata_list(self, asset_metadata_list, user_id, import_only=False):
        """
        Updates both the published and draft branches
        """
        # Convert each asset key to the proper branch before saving.
        asset_keys = [asset_md.asset_id for asset_md in asset_metadata_list]
        for asset_md in asset_metadata_list:
            asset_key = asset_md.asset_id
            asset_md.asset_id = self._map_revision_to_branch(asset_key, ModuleStoreEnum.RevisionOption.published_only)
        super(DraftVersioningModuleStore, self).save_asset_metadata_list(asset_metadata_list, user_id, import_only)
        for asset_md in asset_metadata_list:
            asset_key = asset_md.asset_id
            asset_md.asset_id = self._map_revision_to_branch(asset_key, ModuleStoreEnum.RevisionOption.draft_only)
        super(DraftVersioningModuleStore, self).save_asset_metadata_list(asset_metadata_list, user_id, import_only)
        # Change each asset key back to its original state.
        for k in asset_keys:
            asset_md.asset_id = k

    def _find_course_asset(self, asset_key):
        return super(DraftVersioningModuleStore, self)._find_course_asset(
            self._map_revision_to_branch(asset_key)
        )

    def _find_course_assets(self, course_key):
        """
        Split specific lookup
        """
        return super(DraftVersioningModuleStore, self)._find_course_assets(
            self._map_revision_to_branch(course_key)
        )

    def copy_all_asset_metadata(self, source_course_key, dest_course_key, user_id):
        """
        Copies to and from both branches
        """
        for revision in [ModuleStoreEnum.RevisionOption.published_only, ModuleStoreEnum.RevisionOption.draft_only]:
            super(DraftVersioningModuleStore, self).copy_all_asset_metadata(
                self._map_revision_to_branch(source_course_key, revision),
                self._map_revision_to_branch(dest_course_key, revision),
                user_id
            )
